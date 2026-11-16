"""并行交错验证：不同型号并行审查、同型号并发串行化、工作进程崩溃隔离。

运行（服务无需提前启动；本脚本直接驱动 service 层 + 临时 SQLite）：
    python3 tests/test_parallel.py

覆盖要求：
1. 复现「旧实现」在进程内多线程并发 z3 求解时段错误（回归证据，见 /tmp/repro）；
2. 不同型号的审查在独立工作进程中并行进行，结论/反例各自只属于本次型号与契约；
3. 一个型号的审查尚未结束时，另一型号能够完成审查并发布；网页可查看当前格式；
4. 同型号并发升级仍被行锁串行化，恰好一方成功且落库状态全量兼容；
5. 工作进程原生崩溃时 Web 进程存活、本次发布失败且已发布格式不变、池自动恢复；
6. 不兼容发布仍返回合法反例与拒收程序；兼容才能提交；失败不改已发布格式；
7. 全程全量判定（逐对 z3 证明），无抽样；受限语法不变。
"""
import json
import os
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# 在导入应用模块前固定独立临时数据库，避免污染本地文件
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="parallel-test-"), "contracts.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.setdefault("REVIEW_POOL_SIZE", "3")

from sqlalchemy import select  # noqa: E402

import service  # noqa: E402
from db import (Base, DeviceModel, FirmwareFormat, ProgramFormat, SessionLocal,  # noqa: E402
                engine)
from review import run_review, shutdown_review_pool  # noqa: E402
from schema_spec import message_valid, parse_schema  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)
    print(f"  ✔ {name}")


def temp_doc(lo, hi, qc=None, qc_required=False):
    props = {"temp": {"type": "integer", "minimum": lo, "maximum": hi},
             "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]}}
    required = ["temp", "site"]
    if qc is not None:
        props["qc"] = {"enum": qc}
        if qc_required:
            required.append("qc")
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def create_model(name):
    with SessionLocal() as s:
        s.add(DeviceModel(name=name))
        s.commit()
        return s.execute(select(DeviceModel).where(DeviceModel.name == name)) \
            .scalar_one().id


def state(model_id):
    with SessionLocal() as s:
        fw = s.execute(select(FirmwareFormat).where(
            FirmwareFormat.model_id == model_id).order_by(FirmwareFormat.version)) \
            .scalars().all()
        pg = s.execute(select(ProgramFormat).where(
            ProgramFormat.model_id == model_id).order_by(ProgramFormat.program_id)) \
            .scalars().all()
        return ([(f.version, f.doc) for f in fw], [(p.program_id, p.doc) for p in pg])


def main():
    Base.metadata.create_all(engine)

    # ---------- 1. 进程池基本功能：兼容/不兼容结论与反例归属正确 ----------
    print("1. 进程池审查：结论与反例只属于本次契约")
    fw_wide = parse_schema(temp_doc(-40, 100))
    rd_narrow = parse_schema(temp_doc(-10, 50))
    rd_wide = parse_schema(temp_doc(-40, 100))
    problems = run_review([(1, fw_wide)], [("old", rd_narrow), ("new", rd_wide)])
    ok("不兼容定位到拒收程序", len(problems) == 1
       and problems[0]["program_id"] == "old"
       and problems[0]["firmware_version"] == 1, json.dumps(problems))
    cex = problems[0]["counterexample"]
    ok("反例是合法写方报文且被读方拒收",
       message_valid(cex, fw_wide) and not message_valid(cex, rd_narrow), json.dumps(cex))
    ok("全兼容时问题列表为空", run_review([(1, fw_wide)], [("new", rd_wide)]) == [])

    # ---------- 2. 不同型号并行审查：一个未结束，另一个可完成 ----------
    print("2. 不同型号并行（交错）审查")
    service.test_review_delay_s = 1.5  # 让每个工作进程审查前等待，制造稳定交错
    model_a = f"pa{uuid.uuid4().hex[:8]}"
    model_b = f"pb{uuid.uuid4().hex[:8]}"
    id_a = create_model(model_a)
    id_b = create_model(model_b)
    # 预置：A、B 各有一份固件与程序（当前兼容）
    assert service.publish_firmware(id_a, temp_doc(0, 50)).ok
    assert service.publish_program(id_a, "P", temp_doc(0, 100)).ok
    assert service.publish_firmware(id_b, temp_doc(0, 50)).ok
    assert service.publish_program(id_b, "P", temp_doc(0, 100)).ok

    outcomes = {}
    intervals = {}
    interval_lock = threading.Lock()
    start = threading.Barrier(2)

    def slow_review(model_id, key, doc):
        start.wait()
        t0 = time.time()
        out = service.publish_program(model_id, "P", doc)  # 收窄程序，与现有固件兼容
        with interval_lock:
            outcomes[key] = out
            intervals[key] = (t0, time.time())

    # A 提交一个「会被延迟」的兼容审查；B 紧随其后提交
    t_a = threading.Thread(target=slow_review,
                           args=(id_a, "A", temp_doc(0, 50)))
    t_b = threading.Thread(target=slow_review,
                           args=(id_b, "B", temp_doc(0, 50)))
    wall_t0 = time.time()
    t_a.start(); time.sleep(0.15); t_b.start()
    t_a.join(); t_b.join()
    wall = time.time() - wall_t0
    ok("两个型号都审查成功", outcomes["A"].ok and outcomes["B"].ok,
       str([(k, v.message) for k, v in outcomes.items()]))
    a0, a1 = intervals["A"]
    b0, b1 = intervals["B"]
    overlap = max(0.0, min(a1, b1) - max(a0, b0))
    short = min(a1 - a0, b1 - b0)
    ok("两次审查时间窗真实重叠（并行而非串行）", overlap > 0.8,
       f"overlap={overlap:.2f}s wall={wall:.2f}s")
    # 与机器负载无关的并行证据：较短任务有一半以上时间与另一任务并发执行；
    # 若被串行化，重叠率为 0。绝对墙钟仅记录，不作为阈值（负载下会浮动）。
    ok("较短任务过半时间与另一审查并发（重叠率>50%）", short > 0 and overlap / short > 0.5,
       f"overlap={overlap:.2f}s short={short:.2f}s ratio={overlap / short if short else 0:.2f}")
    ok("结论各自归属：两型号程序均已替换为收窄版",
       all(pid == "P" and doc == temp_doc(0, 50) for pid, doc in state(id_a)[1])
       and all(pid == "P" and doc == temp_doc(0, 50) for pid, doc in state(id_b)[1]))

    # ---------- 3. 一个型号审查耗时较长时，另一型号仍能完成发布 ----------
    print("3. 长审查不阻塞其他型号")
    service.test_review_delay_s = 3.0
    model_c = f"pc{uuid.uuid4().hex[:8]}"
    id_c = create_model(model_c)
    assert service.publish_firmware(id_c, temp_doc(0, 50)).ok
    long_in_worker = threading.Event()
    finish_times = {}

    def long_one():
        long_in_worker.set()
        outcomes["long"] = service.publish_program(id_c, "P", temp_doc(0, 100))
        finish_times["long"] = time.time()

    def quick_one():
        # 等长任务已经把审查送进工作进程后再开始
        long_in_worker.wait()
        t0 = time.time()
        out = service.publish_program(id_b, "P", temp_doc(0, 50))
        outcomes["quick_elapsed"] = time.time() - t0
        outcomes["quick"] = out
        finish_times["quick"] = time.time()

    th_long = threading.Thread(target=long_one)
    th_quick = threading.Thread(target=quick_one)
    th_long.start()
    time.sleep(0.8)  # 确保长任务的审查已在工作进程中执行
    service.test_review_delay_s = 0.0  # 之后的任务不再注入延迟
    th_quick.start()
    th_quick.join()
    th_long.join()
    # 主证据（与负载无关）：快速任务先于仍在 3s 长审查中的任务完成
    ok("快速任务先于长审查完成（不被阻塞）",
       "quick" in finish_times and "long" in finish_times
       and finish_times["quick"] < finish_times["long"],
       f"times={finish_times}")
    # 辅证：快速任务耗时远小于长审查延迟（阈值放宽以容忍机器负载）
    ok("长审查期间另一型号完成发布", outcomes["quick"].ok
       and outcomes["quick_elapsed"] < 2.5, f"elapsed={outcomes.get('quick_elapsed')}")
    ok("长审查本身也成功", outcomes["long"].ok)

    # ---------- 4. 同型号并发升级：行锁串行化，恰好一方成功，状态全量兼容 ----------
    print("4. 同型号并发升级兼容保障")
    m = f"pm{uuid.uuid4().hex[:8]}"
    mid = create_model(m)
    assert service.publish_firmware(mid, temp_doc(0, 50)).ok
    assert service.publish_program(mid, "P", temp_doc(0, 100)).ok
    barrier = threading.Barrier(2)
    results = {}
    for _ in range(5):
        results.clear()

        def narrow():
            barrier.wait()
            results["A"] = service.publish_program(mid, "P", temp_doc(0, 50))

        def widen():
            barrier.wait()
            results["B"] = service.publish_firmware(mid, temp_doc(0, 100))

        barrier = threading.Barrier(2)
        t1 = threading.Thread(target=narrow)
        t2 = threading.Thread(target=widen)
        t1.start(); t2.start(); t1.join(); t2.join()
        codes = [(k, v.ok, bool(v.problems)) for k, v in results.items()]
        exactly_one = sum(1 for v in results.values() if v.ok) == 1
        ok("同型号并发恰好一方成功", exactly_one, str(codes))
        fw_docs, pg_docs = state(mid)
        fw_pairs = [(v, parse_schema(d)) for v, d in fw_docs]
        pg_pairs = [(pid, parse_schema(d)) for pid, d in pg_docs]
        ok("落库状态经全量判定仍然兼容", run_review(fw_pairs, pg_pairs) == [])
        # 还原场景
        assert service.publish_program(mid, "P", temp_doc(0, 100)).ok
        if results["B"].ok:  # 固件只追加，成功过就换新型号重开
            m = f"pm{uuid.uuid4().hex[:8]}"
            mid = create_model(m)
            assert service.publish_firmware(mid, temp_doc(0, 50)).ok
            assert service.publish_program(mid, "P", temp_doc(0, 100)).ok

    # ---------- 5. 不兼容：合法反例 + 拒收程序，且已发布格式不变 ----------
    print("5. 拒绝路径：反例/拒收程序/状态不变/留痕")
    m = f"pd{uuid.uuid4().hex[:8]}"
    mid = create_model(m)
    assert service.publish_firmware(mid, temp_doc(-10, 50)).ok
    assert service.publish_program(mid, "analyzer", temp_doc(-10, 50)).ok
    before = state(mid)
    out = service.publish_firmware(mid, temp_doc(-40, 100))
    ok("不兼容发布被拒绝", (not out.ok) and out.review_failed is False
       and len(out.problems) == 1, out.message)
    p = out.problems[0]
    ok("拒绝依据含拒收程序与合法反例", p["program_id"] == "analyzer"
       and message_valid(p["counterexample"], parse_schema(temp_doc(-40, 100)))
       and not message_valid(p["counterexample"], parse_schema(temp_doc(-10, 50))),
       json.dumps(p))
    ok("拒绝后已发布格式不变", state(mid) == before)

    # ---------- 6. 工作进程原生崩溃：Web 存活、本次失败、状态不变、池恢复 ----------
    print("6. 工作进程崩溃隔离与自动恢复")
    m = f"pe{uuid.uuid4().hex[:8]}"
    mid = create_model(m)
    assert service.publish_firmware(mid, temp_doc(0, 50)).ok
    before = state(mid)
    service.crash_inject_next_review = True  # 下一次审查的工作进程模拟段错误
    crashed_out = service.publish_program(mid, "P", temp_doc(0, 100))
    ok("工作进程崩溃时返回审查失败而非抛出/退出",
       (not crashed_out.ok) and crashed_out.review_failed, crashed_out.message)
    ok("崩溃后已发布格式不变", state(mid) == before)
    ok("注入开关为一次性", service.crash_inject_next_review is False)
    recovered = service.publish_program(mid, "P", temp_doc(0, 100))
    ok("进程池自动恢复后可正常发布", recovered.ok, recovered.message)
    ok("恢复后落库即为新程序", [d for _, d in state(mid)[1]] == [temp_doc(0, 100)])

    # ---------- 6b. 审查超时：放弃本次发布、补建工作进程、后续任务正常 ----------
    print("6b. 审查超时隔离")
    old_timeout = os.environ.get("REVIEW_TIMEOUT_S")
    os.environ["REVIEW_TIMEOUT_S"] = "1"
    m = f"pf{uuid.uuid4().hex[:8]}"
    mid = create_model(m)
    assert service.publish_firmware(mid, temp_doc(0, 50)).ok
    before = state(mid)
    service.test_review_delay_s = 2.0
    timed_out = service.publish_program(mid, "P", temp_doc(0, 100))
    service.test_review_delay_s = 0.0
    ok("超时返回审查失败而非挂起/退出",
       (not timed_out.ok) and timed_out.review_failed and "未完成" in timed_out.message,
       timed_out.message)
    ok("超时后已发布格式不变", state(mid) == before)
    if old_timeout is None:
        os.environ.pop("REVIEW_TIMEOUT_S", None)
    else:
        os.environ["REVIEW_TIMEOUT_S"] = old_timeout
    after_timeout = service.publish_program(mid, "P", temp_doc(0, 100))
    ok("超时补建后新任务正常发布", after_timeout.ok, after_timeout.message)

    # ---------- 7. 高并发不同型号混合压力：无崩溃、无串结论 ----------
    print("7. 多型号并行压力（结论归属校验）")
    models = {}
    for i in range(6):
        nm = f"pf{i}-{uuid.uuid4().hex[:6]}"
        mid_i = create_model(nm)
        assert service.publish_firmware(mid_i, temp_doc(-10, 50)).ok
        assert service.publish_program(mid_i, "P", temp_doc(-10, 50)).ok
        models[nm] = mid_i
    errors, lock = [], threading.Lock()

    def stress(nm, mid_i):
        try:
            # 交替发兼容（程序放宽）与不兼容（固件扩范围）请求
            o1 = service.publish_program(mid_i, "P", temp_doc(-40, 100))
            o2 = service.publish_firmware(mid_i, temp_doc(-40, 100))
            if not o1.ok:
                raise AssertionError(f"{nm} 程序放宽应成功: {o1.message}")
            if not o2.ok:
                raise AssertionError(f"{nm} 固件扩展应成功: {o2.message}")
            # 反例归属：现在故意发一个越界固件，反例必须来自本型号自己的程序 P
            o3 = service.publish_firmware(mid_i, temp_doc(200, 300))
            if o3.ok or o3.problems[0]["program_id"] != "P":
                raise AssertionError(f"{nm} 反例归属错误: {o3.problems}")
            for prob in o3.problems:
                if prob["firmware_version"] != 3:
                    raise AssertionError(f"{nm} 反例固件版本归属错误")
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"{nm}: {e!r}")

    threads = [threading.Thread(target=stress, args=(nm, mid_i))
               for nm, mid_i in models.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok("6 型号并行压力全部通过且结论归属正确", errors == [], json.dumps(errors, ensure_ascii=False))

    shutdown_review_pool()
    print(f"\n全部 {len(PASS)} 项并行交错验证通过")


if __name__ == "__main__":
    main()
