# 水质平台 · 报文契约发布

固件升级前确认新报文能被分析程序读取、程序升级后仍能读旧归档的契约发布网页。
按设备型号登记**历代固件写入格式**（只追加）与**现用程序读取格式**（按程序号替换），
各至多 8 份。每次发布前用 **z3** 对「全部历代写入格式 × 全部现用程序」做**全量**兼容判定
（逻辑证明，非抽样试样例）：任一写入格式允许的全部报文必须被每个现用程序接受，
否则拒绝发布，并给出**合法反例报文**与**拒收程序**。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 操作入口（网页）：http://localhost:8000
- 健康检查：http://localhost:8000/api/health
- 数据保存在命名卷 `pgdata`（PostgreSQL），服务重启后已发布格式保留。

## 网页操作

1. 首页创建设备型号（如 `WQ-5000`），进入型号管理页。
2. 在「发布新格式」粘贴受限 JSON Schema：
   - 左栏**追加固件写入格式**（版本号自动递增，只追加）；
   - 右栏**发布/替换程序读取格式**（同一程序号覆盖旧版）。
3. 发布结果即时展示：成功，或拒绝 + 每个不兼容组合的反例报文与拒收程序；
   页面下方「发布日志」保留最近 50 条成功/拒绝/无效记录（含拒绝依据）。
4. 页面内置三个示例，点按钮即可填入编辑器：
   - **温度扩范围**：固件 v2 扩到 -40~100℃，直接发布会被旧程序（-10~50℃）拒收，
     反例如 `{"temp": -40, ...}`；先升级程序再发固件则通过。
   - **质控码缺省**：固件缺省 `qc`（或写 `null`），把 `qc` 列为 `required` 的程序会被拒，
     反例为不含 `qc` 的报文；程序改为可选并允许 `null` 后通过。
   - **顶层 anyOf**：程序同时接受多种报文形态。

## 受限 JSON Schema 语法（其余一律拒绝）

- 平面对象：`{"type":"object","properties":{...},"required":[...],"additionalProperties":false}`
  （`required` 可省略，默认空；`additionalProperties` 必须显式为 `false`）
- 顶层 `anyOf`：`{"anyOf":[<平面对象>, ...]}`，且只能单独出现在顶层
- 整数上下界：`{"type":"integer","minimum":m,"maximum":M}`（上下界均可省略）
- 字符串/整数/空值枚举：`{"enum":[...]}`，可配 `"type"` 限定；枚举值只允许字符串、整数、`null`
- 空值类型：`{"type":"null"}`

嵌套对象、数组、`pattern`、`oneOf`/`allOf`/`not`、浮点/布尔值、`string` 无枚举等语法均被拒绝。

## 校验原理

对每对（写入格式 W，读取格式 R），把报文编码为 z3 变量（每个字段：是否出现、
值类型 null/int/string、整数值、字符串枚举下标），求解 `W 合法 ∧ R 拒收`：

- **UNSAT** ⇒ 不存在反例，`L(W) ⊆ L(R)` 成立——这是对全部报文的证明，不是抽样；
- **SAT** ⇒ 从模型还原一个反例报文，并用独立的纯 Python 判定器复核
  （确实满足 W、确实被 R 拒收）后随拒绝结果返回。

每次求解使用独立的 `z3.Context`（不触碰进程级全局 main_ctx），且整段审查
在 `review.py` 的独立 spawn 工作进程内执行，从根本上避免多线程共享 Z3
context 导致的段错误。

## 并发与事务

- **进程隔离的兼容审查**：z3 的 Python 绑定默认共享进程级全局 Z3 context，
  Web 进程内多线程并发求解会破坏其原生层状态并触发**段错误**，使发布进程退出。
  因此每次全量兼容审查都被投递到独立的 **spawn 常驻守护工作进程池**执行
  （每次求解另建独立 `z3.Context`，进程间只传递普通数据，不传递任何 z3 对象）：
  - 不同型号的审查在不同工作进程中**真正并行**，一个型号长求解不阻塞其他型号，
    网页与查询接口全程可查看当前已发布格式；
  - 工作进程若原生崩溃（段错误等），只终结该工作进程：父进程摘除并立即补建
    新进程，本次审查按失败处理（HTTP 500、已发布格式不变、日志留痕），Web 进程存活；
  - 审查整体超时走同一摘除/补建路径，绝不带着悬挂任务复用进程；
  - 工作进程均为 daemon，关闭时不 join、不发信号（避免未排空队列的 join 死锁与
    信号受限环境的 PermissionError），由操作系统在父进程退出时回收；退出前由
    `review.py` 的 atexit 处理器把它们从 multiprocessing 子进程登记中摘除。
- **锁外审查 + 短事务复核**：审查期间**不持有数据库锁**（避免长求解把不同型号
  也串行化）。先读只读快照并在工作进程审查，通过后才开短事务：锁定型号行 →
  重新读取 → 复核快照未被同型号并发提交改变 → 落库；快照过期则放弃本次结论并重试。
- 由此**同型号并发升级仍被严格串行化**（行锁 + 快照复核 + 有限重试），
  不会各自通过却留下不兼容组合；检查失败整体回滚，**已发布格式不变**；
  拒绝/无效/审查失败尝试另写发布日志留痕。
- 固件格式只追加（无修改/删除入口），程序格式按程序号替换；各至多 8 份。

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/models` | 创建型号 `{"name": "WQ-5000"}` |
| GET | `/api/models/{name}` | 查看当前固件/程序格式 |
| GET | `/api/models/{name}/log` | 发布日志（含拒绝依据） |
| POST | `/api/models/{name}/firmware` | 追加固件格式 `{"schema": {...}}` |
| POST | `/api/models/{name}/programs` | 发布/替换程序格式 `{"program_id": "...", "schema": {...}}` |

响应：成功 `200 {"ok": true, ...}`；不兼容 `409 {"ok": false, "problems": [{firmware_version, program_id, counterexample}]}`；语法/上限错误 `422`。

## 本地开发与自测

```bash
pip install -r app/requirements.txt httpx
python3 tests/test_units.py                 # 语法子集 + z3 判定单元自测
python3 tests/test_parallel.py              # 并行交错：跨型号并行/同型号串行/崩溃隔离/超时
cd app && DATABASE_URL=sqlite:////tmp/contracts.db uvicorn main:app --port 8000 &
python3 tests/selftest.py                   # 集成自测（示例/上限/并发/页面）
```

`DATABASE_URL` 缺省为 SQLite（便于本地自测）；Compose 中注入 PostgreSQL 连接串。
`tests/test_parallel.py` 直接驱动 service 层与审查进程池，使用独立临时库，
通过任务参数注入工作进程延迟（制造可复现交错）与原生崩溃（验证进程隔离与自愈），
不依赖也不会修改生产配置。

## 目录结构

```
app/
  main.py         FastAPI 入口（HTML 页面 + JSON API）
  service.py      锁外审查 -> 短事务加锁复核 -> 落库/回滚（含同型号并发重试）
  review.py       spawn 工作进程池：进程隔离、崩溃/超时自愈的兼容审查调度
  solver.py       z3 编码与全量兼容判定（独立 Context，含反例复核）
  schema_spec.py  受限 JSON Schema 子集解析与纯 Python 报文判定
  db.py           SQLAlchemy 模型（PostgreSQL JSONB / SQLite JSON）
  examples.py     温度扩范围、质控码缺省、anyOf 示例
  templates/      页面模板
tests/
  test_units.py     单元自测
  test_parallel.py  并行交错验证（跨型号并行、同型号串行、崩溃/超时隔离）
  selftest.py       集成自测
docker-compose.yml
```
