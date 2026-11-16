"""兼容审查的进程隔离层。

背景：z3 的 Python 绑定默认共享进程级全局 Z3 context，Web 进程内多线程
并发求解会破坏其原生层状态并触发段错误，导致整个发布进程退出。

做法：把每次全量兼容审查（check_compatibility，纯函数、不碰数据库）整体
投递到 **spawn 常驻工作进程**执行——
- 不同型号的审查可在不同工作进程中**并行**（不把所有型号串行化）；
- 同一型号的两次发布由 service 层的行锁 + 快照复核串行化，兼容保障不变；
- 工作进程内的任何原生崩溃（段错误等）只终结该工作进程：父进程回收并
  补建一个新工作进程，Web 进程不受影响，本次审查按失败处理；
- 审查失败/超时一律抛 ReviewError，由发布事务回滚，已发布格式不变。

不使用 concurrent.futures.ProcessPoolExecutor：它的内部管理线程在工作进程
崩溃时会对兄弟进程调用 terminate()，在信号受限环境（部分容器/沙箱）里可能
抛 PermissionError 且其 atexit join 可能挂住退出流程。这里父进程直接持有
子进程对象，崩溃检测、补建与关闭路径完全可控。

进程间只传递 Schema（普通可 pickle 数据类）与基础类型，不传递任何 z3 对象。
"""
from __future__ import annotations

import atexit
import multiprocessing as mp
import multiprocessing.process as mp_process
import os
import queue
import threading
import uuid

from solver import check_compatibility


def _forget_child(proc):
    """从 multiprocessing 内部子进程登记中摘除，避免解释器 atexit 再去
    terminate 它（在信号受限环境会抛 PermissionError 并干扰退出）。"""
    try:
        mp_process._children.discard(proc)
    except Exception:
        pass


class ReviewError(RuntimeError):
    """审查未能完成（工作进程崩溃/超时/内部异常）。不代表发现不兼容。"""


class _WorkerDied(Exception):
    """内部信号：承担任务的工作进程在回包前死亡。"""


class _Timeout(Exception):
    """内部信号：等待审查回包超过整体时限。"""


def _pool_size() -> int:
    try:
        n = int(os.environ.get("REVIEW_POOL_SIZE", ""))
        if n >= 2:
            return n
    except ValueError:
        pass
    return max(2, min(4, os.cpu_count() or 2))


def _review_timeout_s(n_pairs: int) -> float:
    """单次审查总时限：每对求解自身有 15s z3 超时，这里再加一层整体兜底。"""
    try:
        return float(os.environ["REVIEW_TIMEOUT_S"])
    except (KeyError, ValueError):
        return max(60.0, 30.0 * n_pairs)


def _worker_loop(task_q, result_q):
    """常驻工作进程主循环：只做纯计算，不访问数据库/网络。

    全部为 daemon 进程：父进程退出时由操作系统回收，无需父进程发信号或 join，
    从而既避免信号受限环境的 PermissionError，也避免未排空队列就 join 的死锁。
    """
    while True:
        try:
            task = task_q.get()
        except (EOFError, OSError, BrokenPipeError):
            return  # 队列已关闭/断开：自行结束
        if task is None:  # 优雅停止信号
            return
        job_id, fw_pairs, prog_pairs, crash_inject, delay_s = task
        try:
            if crash_inject:  # pragma: no cover - 测试分支
                import ctypes
                ctypes.string_at(0)
            if delay_s > 0:
                import time
                time.sleep(delay_s)
            result = check_compatibility(fw_pairs, prog_pairs)
            result_q.put((job_id, None, result))
        except Exception as e:  # 业务异常（含 z3 超时）回传父进程
            try:
                result_q.put((job_id, f"{type(e).__name__}: {e}", None))
            except Exception:
                pass


class _Worker:
    """一个常驻 spawn daemon 工作进程及其独占任务/结果队列。"""

    def __init__(self, ctx):
        self.task_q = ctx.Queue()
        self.result_q = ctx.Queue()
        self.proc = ctx.Process(target=_worker_loop,
                                args=(self.task_q, self.result_q), daemon=True)
        self.proc.start()
        self.busy = False

    def alive(self):
        return self.proc.is_alive()

    def detach(self):
        """释放对该工作进程的全部引用：不 join、不 terminate。

        - 先尽力投递停止信号（投递失败也无妨）；
        - 从 multiprocessing 的 _children 登记摘除，使 stdlib atexit 不再
          对其调用 terminate（信号受限环境会 PermissionError）；
        - 关闭队列；daemon 进程在父进程退出时由操作系统回收。
        """
        try:
            self.task_q.put(None)
        except Exception:
            pass
        _forget_child(self.proc)
        for q in (self.task_q, self.result_q):
            try:
                q.close()
            except Exception:
                pass

    def close(self):
        self.detach()


class _ResultBox:
    """单次审查的回包收件箱。"""

    def __init__(self):
        self._evt = threading.Event()
        self._value = None

    def deliver(self, value):
        self._value = value
        self._evt.set()

    def wait(self, timeout):
        if not self._evt.wait(timeout):
            raise _Timeout()
        return self._value


class _ReviewPool:
    """懒加载、可自愈的 spawn 工作进程池；所有方法线程安全。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._ctx = mp.get_context("spawn")  # 不用 fork：避免继承多线程父进程状态
        self._workers = []
        self._size = _pool_size()

    def _ensure_workers_locked(self):
        """补建任何已死亡/缺失的工作进程（调用方持锁）。"""
        spawned = False
        alive = [w for w in self._workers if w.alive()]
        if len(alive) != len(self._workers):
            for w in self._workers:
                if not w.alive():
                    w.detach()
            self._workers = alive
        while len(self._workers) < self._size:
            self._workers.append(_Worker(self._ctx))
            spawned = True
        if spawned:
            # spawn 可能把 multiprocessing 的退出处理器重新排到末尾，
            # 这里把我们的关闭处理器重新放回末尾以保证最先执行
            _install_atexit_last()

    def _acquire_worker_locked(self):
        """取一个空闲且存活的工作进程；全忙则等待（调用方持锁）。"""
        while True:
            self._ensure_workers_locked()
            for w in self._workers:
                if w.alive() and not w.busy:
                    w.busy = True
                    return w
            self._cond.wait(timeout=1.0)

    def _release_locked(self, worker):
        if worker in self._workers:
            worker.busy = False
        self._cond.notify_all()

    def _discard_worker(self, worker):
        """工作进程崩溃/超时后摘除并立即补建，保持池的并行度。"""
        with self._cond:
            if worker in self._workers:
                self._workers.remove(worker)
            worker.detach()
            self._ensure_workers_locked()
            self._cond.notify_all()

    def submit_review(self, fw_pairs, prog_pairs, crash_inject=False, delay_s=0.0):
        n_pairs = len(fw_pairs) * len(prog_pairs)
        timeout = _review_timeout_s(max(1, n_pairs))
        job_id = uuid.uuid4().hex
        with self._cond:
            worker = self._acquire_worker_locked()
            worker.task_q.put((job_id, fw_pairs, prog_pairs, crash_inject, delay_s))
            box = _ResultBox()
        reader = threading.Thread(target=self._read_loop,
                                  args=(worker, job_id, box), daemon=True)
        reader.start()
        try:
            value = box.wait(timeout)
        except _Timeout:
            # 超时与崩溃同路径：丢弃该工作进程并补建，绝不带着悬挂任务复用
            self._discard_worker(worker)
            raise ReviewError(
                f"兼容审查超过 {timeout:.0f}s 未完成，已终止该审查并补建工作进程，"
                "本次发布未生效")
        with self._cond:
            self._release_locked(worker)
        if isinstance(value, _WorkerDied):
            self._discard_worker(worker)
            if crash_inject:
                # 确定性崩溃注入：不重试，仅保证池已补建
                raise ReviewError("兼容审查工作进程崩溃，已补建新进程，本次发布未生效")
            return self.submit_review(fw_pairs, prog_pairs,
                                      crash_inject=False, delay_s=delay_s)
        if isinstance(value, Exception):
            raise ReviewError(f"兼容审查失败：{value}")
        return value

    def _read_loop(self, worker, job_id, box):
        """专线程等待该工作进程的回包；进程死亡则交付 _WorkerDied。"""
        try:
            while True:
                try:
                    rid, err, result = worker.result_q.get(timeout=0.5)
                except queue.Empty:
                    if not worker.alive():
                        box.deliver(_WorkerDied())
                        return
                    continue
                if rid != job_id:  # 不应发生：每 worker 同一时刻只跑一个任务
                    box.deliver(_WorkerDied())
                    return
                box.deliver(RuntimeError(err) if err is not None else result)
                return
        except Exception as e:
            box.deliver(e)

    def shutdown(self):
        with self._cond:
            for w in self._workers:
                w.close()
            self._workers = []


_pool = _ReviewPool()


def run_review(fw_pairs, prog_pairs, crash_inject=False, delay_s=0.0):
    """在独立工作进程执行全量兼容审查，返回问题列表（空列表=全部兼容）。

    审查失败（工作进程崩溃/超时/内部错误）抛 ReviewError；
    调用方必须按事务失败处理（回滚，不改已发布格式）。
    crash_inject / delay_s 仅供测试：分别模拟工作进程原生崩溃、制造并行交错。
    """
    return _pool.submit_review(fw_pairs, prog_pairs,
                               crash_inject=crash_inject, delay_s=delay_s)


def shutdown_review_pool():
    _pool.shutdown()


def _install_atexit_last():
    """把关闭处理器重新放到 atexit 队列末尾（LIFO 最先执行）。

    multiprocessing 在 spawn 工作进程期间会经 get_logger 把自身的
    _exit_function 取消后重新注册（挪到队列末尾），若不重排，它会先于我们
    运行并尝试 terminate 守护进程，在信号受限环境抛 PermissionError。
    每次补建工作进程后调用一次，保证我们始终最先执行、先摘除子进程。
    """
    atexit.unregister(shutdown_review_pool)
    atexit.register(shutdown_review_pool)


# 模块导入时先注册一次；实际生效顺序由 _install_atexit_last 在每次 spawn 后重排保证。
atexit.register(shutdown_review_pool)
