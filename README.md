# 设备校准参数发布 · 预备事务恢复台 + 发布台名额/抢修放行（FastAPI + PostgreSQL + SQLite）

设备校准参数通过 2PC（两阶段提交）发布，协调端与参与者之间可能在任意时刻中断。
本项目提供一个值班恢复网页：按**参数号 / 设备号 / 事务号**查询，依据 **SQLite 中协调端留下的
COMMIT/ABORT 决定**收尾 PostgreSQL 的预备事务；当网页自己还没登记就退出、重启后，则完全依据
**数据库证据**区分该事务最终是已提交还是已回滚。

同时内置一个**发布台名额**子系统（导航「🚨 发布台名额/抢修放行」，路径 `/publish`，首页也有名额摘要）：
总名额可在 PostgreSQL `max_prepared_transactions` 上限内配置，其中一席固定预留给指定抢修设备。
名额就是 PostgreSQL 事务级 advisory lock —— 一笔发布从建单受理到库端收尾只占一席，
**等参数行锁时也占席**；普通并发建单满额立即拒绝；普通发布等锁时抢修设备仍可走预留席；
PREPARE 前失败/进程退出立即还席；PREPARE 后网页重启仍占席，**库端直接 COMMIT/ROLLBACK
PREPARED 后由 PostgreSQL 自动还席，无需网页补登记**。Docker Compose 默认用 **2 个名额**演示抢修放行。

---

## 1. 它如何判定事务状态

一个全局事务号 `gxid` 同时用作 PostgreSQL 的预备事务标识符（`PREPARE TRANSACTION 'gxid'`）。

| 证据 | 含义 |
| --- | --- |
| `pg_prepared_xacts` 仍有该 `gxid` | 事务仍 PREPARED，**行锁/表锁仍保留**，可执行 `COMMIT/ROLLBACK PREPARED` |
| `txn_commit_marker` / `param_revision` 中有该 `gxid` | 这两类行只在预备事务内部写入，可见即**强证据：已提交** |
| `publish_order` 发布单仍在（PREPARE 前独立提交），当前值=基线前像值、且 `updated_gxid<>gxid` | 与“**已回滚**”相互印证 |
| 发布单缺失 / 基线值已被后续校准覆盖 / 证据互相矛盾 | **证据不足 → 待核实（UNVERIFIED）**，不臆断 |
| SQLite `coord_decision` | 协调端留下的不可变 `COMMIT/ABORT` 决定 |
| SQLite `recovery_record` | 网页自己的办理登记簿（幂等兜底） |

由此得到页面状态：

- `OPEN_AWAITING` 挂起中·**待确认决定**：事务和锁原样保留，拒绝擅自收尾；值班员可在页面人工补录决定后收尾。
- `OPEN_COMMIT` / `OPEN_ABORT`：按 SQLite 决定一键 `COMMIT/ROLLBACK PREPARED`。
- `RECONCILE_COMMIT` / `RECONCILE_ABORT`：**库端已收尾、网页未登记**（例如登记前进程退出，重启后），凭证据补登记，**不再改参数**。
- `CONFLICT_COMMIT_BUT_ABORTED` / `CONFLICT_ABORT_BUT_COMMITTED`：决定与库端事实冲突，红色指出并登记冲突，转人工补偿。
- `UNVERIFIED`：证据不足，标“待核实”，由值班员填人工核实结论。
- `DONE_*`：已登记，重复办理一律幂等忽略。

**幂等保证**：收尾在 SQLite `BEGIN IMMEDIATE` 临界区内检查登记簿，`recovery_record` 主键兜底；
数据库侧 `param_revision.gxid UNIQUE`、`txn_commit_marker.gxid PRIMARY KEY` 兜底。
恢复/补登记路径不会重新执行参数 `UPDATE`，因此重复点击、并发点击、重复 HTTP 调用都不会再次修改参数。

---

## 2. 目录结构

```
docker-compose.yml          db(postgres:16, max_prepared_transactions=100) + web(PUBLISH_MAX_SLOTS=2)
Dockerfile                  python:3.12-slim + uvicorn 单 worker
db/01_schema.sql            表结构 + 5 条内置校准参数样例（含集中发布/抢修专用两行）
app/
  main.py                   FastAPI 页面/表单/JSON 路由（含 /publish 名额台与 /api/slots）
  config.py                 环境变量配置（PUBLISH_MAX_SLOTS / EMERGENCY_DEVICE_NO ...）
  slots.py                  席位布局、pg_try_advisory_xact_lock 分配、pg_locks 实时名额看板
  publish.py                占席下单 2PC 流程、行锁道具、后台任务、一键抢修演示、库端直接收尾
  pgdb.py                   PG 连接、等待就绪、幂等建表
  sqlite_store.py           协调决定库 + 网页登记簿/审计事件
  recovery.py               取证、状态分类、收尾/补登记/冲突/人工核实、锁演示
  sample.py                 内置参数发布流程 + 8 种中断现场构造
  coord_cli.py              协调端命令行：向 SQLite 写 COMMIT/ABORT 决定
  templates/ publish.html … 服务端渲染中文页面（名额台 2s 轮询 /api/slots）
  static/ style.css
scripts/e2e_check.py        恢复台 49 项 HTTP 端到端核对（含可选崩溃/重启流程）
scripts/slot_check.py       名额/抢修放行 23 项 HTTP 关键场景核对
tests/test_recovery.py      pytest：恢复台 12 个关键场景
tests/test_slots.py         pytest：名额/预留/等锁/满额拒绝/表单与JSON/失败还席/重启与库端收尾 13 个场景
```

两个 SQLite 库分开挂载，语义不同：

- `COORD_SQLITE_PATH`（默认 `/data/coord/coordinator.db`）：**协调端**决定日志，只增不改。
- `WEB_SQLITE_PATH`（默认 `/data/web/recovery.db`）：**恢复网页**的登记簿与审计时间线。

---

## 3. Docker Compose 启动（推荐交付方式）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- PostgreSQL 暴露在 `localhost:5432`（`recovery / recovery_pw / caldb`）。
- 网页在 `http://localhost:8000`，接口文档 `http://localhost:8000/docs`。
- `restart: unless-stopped` 且 `ENABLE_CRASH_DEMO=true`，用于演示“收尾后、登记前进程退出，Compose 自动重启”。

### 值班演示剧本

0. **名额/抢修放行（本次新增）**：打开 <http://localhost:8000/publish>，点「▶ 运行抢修放行剧本」，
   看第 1 普通席等锁占额、第二笔普通建单被拒（含原因码）、抢修设备走第 2 预留席完成更新；
   再释放行锁、库端直接撤销，看名额自动归还。详见第 5 节。
1. 首页「① 构造样例」选择 **挂起③：…决定尚未写入…** → 恢复页显示黄色「待确认决定」，
   证据区列出该预备事务持有的 `RowExclusiveLock` / `ExclusiveLock(transactionid)`。
   - 参数详情页可点「模拟其他会话修改此参数」，会得到 `55P03 lock_not_available`，证明锁仍在（测试会话一定回滚）。
   - 此时直接调收尾接口会被拒绝；事务与锁保持不动。
   - 在页面下方填写核对说明，人工补录 `COMMIT` 或 `ABORT` 并收尾。
2. 构造 **挂起①/②**（SQLite 已有决定）→ 点按钮按决定 `COMMIT/ROLLBACK PREPARED`；
   再点「重复调用收尾接口」，提示幂等、参数值/版本/修订条数不变。
3. 构造 **重启④/⑤（库端已提交/已回滚，但网页没登记）**：
   直接进事务页即可看到系统凭数据库证据判定「库端已提交/已回滚 · 网页未登记」，点「补登记」。
   也可以在容器里真实重启网页验证：
   ```bash
   docker compose restart web
   ```
4. 真实崩溃演示：在 ①/② 类事务的收尾区勾选
   「收尾后、登记前立即退出进程」再点收尾。网页进程以 42 退出，Compose 自动拉起；
   刷新该事务页，会出现「库端已提交 · 网页未登记」，补登记即可。
5. 构造 **冲突⑥/⑦**：红色横幅指出“决定提交/撤销，但库端实际相反”，填写工单号登记冲突（不改参数）。
6. 构造 **待核实⑧**：回滚后基线值又被后续校准覆盖，前像证据失效 → 标「待核实」，由专家填写结论。
7. 顶部搜索框可按参数号（如 `CAL-T-1001`）、设备号（`DEV-P-07`）、事务号（`GX-...`）查询。

### 协调端 CLI（模拟真正的协调器留决定）

```bash
docker compose exec web python -m app.coord_cli list
docker compose exec web python -m app.coord_cli decide GX-xxxx COMMIT --reason "校验通过"
docker compose exec web python -m app.coord_cli decide GX-xxxx ABORT  --reason "超差"
```

决定一旦写入不可更改（重复 `decide` 返回原决定）。

### 一键 HTTP 核对

```bash
# 服务起来后，宿主机或任意能访问 8000 的机器：
python3 scripts/e2e_check.py --base-url http://localhost:8000

# 含真实崩溃/Compose 重启的完整核对（约 1 分钟）：
python3 scripts/e2e_check.py --base-url http://localhost:8000 --crash-flow
```

---

## 4. 裸机/本地试跑（无 Docker 时）

需要 Python 3.11+、一个 `max_prepared_transactions>0` 的 PostgreSQL：

```bash
python3 -m pip install -r requirements.txt
export DATABASE_URL='postgresql://user:pass@localhost:5432/caldb'
export COORD_SQLITE_PATH=./data/coordinator.db
export WEB_SQLITE_PATH=./data/recovery.db
export ENABLE_CRASH_DEMO=true
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 测试（可通过 TEST_DATABASE_URL 指向测试库）
TEST_DATABASE_URL='postgresql://.../caltest' python3 -m pytest -q
```

---

## 5. 发布台名额与抢修放行（两席演示）

打开 <http://localhost:8000/publish>（或点顶部「🚨 发布台名额/抢修放行」）。Compose 已配置
`PUBLISH_MAX_SLOTS=2`、`EMERGENCY_DEVICE_NO=DEV-EMG-01`，因此：

- **第 1 席＝普通席**（编号 `1 .. N-1`），普通设备与抢修设备都可用；
- **第 2 席＝抢修预留席**（最后一席 `N`），只对 `DEV-EMG-01` 开放，普通设备的候选集里根本没有它。

### 5.1 名额为什么“重启不丢、库端一收尾就还”

每个席位对应一把两参数事务级 advisory lock（命名空间 `75010001`，第二个 key 为席号）：

| 时刻 | 席位锁状态 |
| --- | --- |
| 建单受理 `SELECT pg_try_advisory_xact_lock(...)` | 立即非阻塞占席；抢不到合格空席立刻拒绝 |
| 等参数行锁的 `UPDATE` 阻塞中 | advisory 锁仍在本事务手里 → **等锁也算占一席** |
| `PREPARE TRANSACTION` 成功 | 锁移交给预备事务，`pg_locks` 中表现为 `granted=true, pid=NULL`，并可经 `virtualtransaction → transactionid → pg_prepared_xacts.gid` 映射回事务号 |
| 网页重启 / 进程消失（已 PREPARE） | 预备事务与锁由 PostgreSQL 保留 → **仍占额**，`/api/slots` 是纯 PG 查询，新进程照样看得到 |
| 任意会话 `COMMIT/ROLLBACK PREPARED` | PostgreSQL 自动释放锁 → **席位立刻归还，不需要网页补登记** |
| PREPARE 前失败 / `rollback` / 连接断开 / 进程 `_exit` | 事务回滚，xact advisory lock 自动释放 → **立即还席** |

剩余名额＝`PUBLISH_MAX_SLOTS − pg_locks 中本命名空间已授予的席位锁数`，因此不依赖网页内存，
多副本/重启后都准确（本演示按单 worker 运行）。总名额启动/查询时会与
`SHOW max_prepared_transactions`、`max_connections` 比对，超上限在页面红色指出。

### 5.2 一键剧本（推荐先点这个）

在 `/publish` 红色卡片点 **「▶ 运行抢修放行剧本」**，服务端会在约 1 秒内自动：

1. 用独立会话 `SELECT ... FOR NO KEY UPDATE` 占住 `CAL-BULK-1001` 的行锁；
2. 后台受理第一笔普通发布（`DEV-BULK-01`）→ 拿到第 1 席，随后阻塞在参数 `UPDATE`，看板上该席变红「正在等参数行锁（占席）」；
3. 受理第二笔普通建单（`DEV-BULK-02`）→ 普通席已满，返回拒绝码 `GENERAL_SLOTS_FULL`
   与中文原因（明确说明第 2 席是抢修预留席、普通设备不可用）；
4. 抢修设备 `DEV-EMG-01` 建单 → 候选顺序 `[2, 1]`，直接用第 2 席发布自己的 `CAL-EMRG-0001`
   （无行锁冲突），写完 2PC 并按 COMMIT 决定快速提交，校准值 `8.8880 → 8.90170 …` 立即可见，提交后第 2 席归还。

战报、拒绝原因码、两笔事务号都会列在页面上。随后：

- 点 **「② 释放行锁，让普通发布继续到 PREPARE」**：等待者被唤醒并进入 PREPARE，第 1 席转为黄色「预备态（重启仍占额）」；
- 在看板该行点 **「库端直接撤销 / 提交」**（等价 DBA 在 psql 里直接 `ROLLBACK/COMMIT PREPARED`，
  **不写网页登记簿**）：席位立刻变空闲；再进事务页会看到既有的「库端已回滚/已提交 · 网页未登记」，可照常补登记。

页面还提供：**③ 模拟建单失败（未 PREPARE，立即还席）**、**④ PREPARE 前崩溃退出**
（需 `ENABLE_CRASH_DEMO=true`，进程以 43 退出，Compose `restart: unless-stopped` 拉起后看板应显示席位已归还）。

### 5.3 手工/接口演示与核对

```bash
# 实时名额快照（total/remaining/general_*/reserved_*/slots[].phase,gxid,...）
curl -s http://localhost:8000/api/slots | python3 -m json.tool

# JSON 受理发布；满额返回 409，body 含 code / error / slots
curl -s -X POST http://localhost:8000/api/publish/orders \
  -H 'Content-Type: application/json' \
  -d '{"device_no":"DEV-BULK-01","param_no":"CAL-BULK-1001"}'

# 23 项名额/抢修 HTTP 断言（服务起来后任意机器可跑，仅标准库）
python3 scripts/slot_check.py --base-url http://localhost:8000
```

配置项（均见 `.env.example`）：

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `PUBLISH_MAX_SLOTS` | `2` | 总名额，须 `1 ≤ N ≤ max_prepared_transactions`（Compose 库为 100），且低于 `max_connections` 安全余量 |
| `EMERGENCY_DEVICE_NO` | `DEV-EMG-01` | 指定抢修设备；多台可用 `EMERGENCY_DEVICE_NOS=A,B,C` |
| `PUBLISH_LOCK_TIMEOUT_MS` | `15000` | 网页普通下单等参数行锁的上限；`0`＝死等。一键剧本的后台任务用 120s 上限 |

---

## 6. 交付核对：已完成 / 已实测 / 未完成

### 已完成（代码与交付物）

- FastAPI 恢复页 + JSON 接口 + 服务端渲染中文 UI；Dockerfile、docker-compose.yml、初始化 SQL、`.env.example`。
- PostgreSQL 2PC 全流程样例：发布单（前像/目标值）、预备事务、参数修订表、提交标记表；3 条内置校准参数。
- SQLite 协调决定库（不可变）与网页登记簿/审计事件流。
- 按参数号/设备号/事务号查询；参数详情与发布历史；预备事务锁清单与“他人修改被阻塞”演示。
- 8 种一键中断现场：有决定提交/回滚、无决定待确认、库端已提交/已回滚但未登记、两种决定-事实冲突、证据不足待核实。
- 收尾、证据补登记、冲突登记、人工核实、无决定时人工补录并收尾；全链路幂等与并发串行化。
- 崩溃演示开关（`os._exit(42)`，恰在 PG 收尾后、SQLite 登记前），配合 Compose `restart` 演示重启恢复。
- **发布台名额/抢修放行（本次新增，详见第 5 节）**：`app/slots.py`（席位布局 + advisory xact lock 分配 +
  纯 PG 实时看板）、`app/publish.py`（占席 2PC 下单、行锁道具、后台等锁任务、一键抢修剧本、库端直接收尾）、
  `/publish` 中文看板（2s 轮询剩余名额/各席阶段/拒绝原因/库端收尾按钮）、`/api/slots` 与
  `POST /api/publish/orders`（满额 409 + 原因码）。
- `scripts/e2e_check.py`、`scripts/slot_check.py`、`tests/test_recovery.py`（12 例）、`tests/test_slots.py`（12 例）。

### 已在本环境实际试跑

本环境**没有 Docker 守护进程/CLI**，因此用 PyPI 上的 `pgserver` 内置 PostgreSQL 16.2
（与 `postgres:16` 大版本一致；一次性 `initdb` 配置 `max_prepared_transactions=100`）
+ 本机 uvicorn 做了等价实测。所有命令均在 `/workspace` 下、用一次性全新 PG 实例复跑：

- **`python3 -m pytest -q`：25 passed**（`tests/test_recovery.py` 12 + `tests/test_slots.py` 13）。
  - 恢复台 12 例：全部页面渲染、无决定保留事务与锁并拒绝收尾、人工提交/回滚、按决定收尾、
    重复办理参数不变、6 线程并发收尾恰好 1 次生效、提交/回滚未登记的证据补登记、
    两种冲突、待核实与人工结论、锁探测不改数据。
  - 名额 13 例：2 席布局与 PG 上限校验、普通满额 `GENERAL_SLOTS_FULL`、抢修走第 2 预留席、
    6 线程并发普通建单恰好 1 单成功、**普通发布等参数行锁时仍占第 1 席且抢修可用第 2 席**、
    未 PREPARE 失败立即还席、**PREPARE 后清空进程内登记表（等价重启）仍显示占席、库端直接
    ROLLBACK/COMMIT PREPARED 后席位清零且 SQLite 无登记簿**、`/publish` 与 `/api/slots`、
    HTML 表单满额 303 中文拒绝、JSON 409/`ALL_SLOTS_FULL`、一键 HTTP 抢修剧本端到端。
  - `tests/conftest.py` 增加了清表前 `pg_terminate_backend('calpub%'/'lock-demo')` 与 5s
    `lock_timeout` 兜底，并把会刻意制造锁等待会话的 `test_lock_demo_*` 排到会话最后，
    避免本沙箱信号受限导致的跨用例清表阻塞；普通 Docker/Linux 上 terminate 兜底已足够。
- **真实 HTTP 端到端（本机 uvicorn + 全新 PG）**：
  - `python3 scripts/slot_check.py`：**23 项断言全部通过**（名额布局、一键抢修战报与
    `GENERAL_SLOTS_FULL`、看板观察到 `waiting_lock`、释放行锁后转 `prepared`、
    DBA 直接回滚还席、失败还席、JSON 200/409/`ALL_SLOTS_FULL`、收尾清零）。
  - `python3 scripts/e2e_check.py`（不含 `--crash-flow`）：**49 项断言全部通过**，确认新增功能未回归恢复台。
- **重启 / 库端直接收尾 / 预备前崩溃专项（一次性脚本，`VERIFY2_OK`）**：
  A 解释器造一笔 PREPARED 占席发布 → **全新 B 解释器（等价重启后的网页）**的 `slots.status()`
  仍读到该席 `prepared` → psql 直接 `ROLLBACK PREPARED` → 新读 `used=0 remaining=2`（全程未调网页登记）；
  子进程以 `CRASH_GXID` 执行到 PREPARE 前 `os._exit(43)`（实测退出码 43）→ 该 gxid 不在
  `pg_prepared_xacts`、本命名空间 advisory 锁为 0，证明预备前中途退出立即还席。
- 既有崩溃/重启数据正确性专项（`os._exit(42)` 于 PG 收尾后、SQLite 登记前，再用全新解释器取证）：
  **ALL_OK**——COMMIT/ABORT 两路线重启后分别判定 `COMMITTED/RECONCILE_COMMIT`、
  `ROLLED_BACK/RECONCILE_ABORT`，补登记不改参数、重复补登记幂等。
- 协调 CLI 的写入/列表/决定不可变也已实测。
- 说明：本沙箱会限制 PostgreSQL 跨进程信号（实测对“跨 Shell 调用遗留”的实例
  `pg_terminate_backend` 返回 false、服务端 `lock_timeout` 偶发不触发、遗留锁等待唤醒偶发丢失；
  普通 Docker/Linux 不存在该限制）。因此验证脚本都在**同一次 Shell 调用内**拉起一次性 PG 并跑完，
  名额状态只依赖 `pg_locks/pg_prepared_xacts`，并发正确性由 PG advisory lock 原子保证。

### 未完成 / 需在有 Docker 的机器上补验

- **未在本机构建/启动 `docker compose`**（环境无 `docker` 可执行文件与守护进程）。
  `docker-compose.yml` 已做 YAML 解析，新增的 `PUBLISH_MAX_SLOTS/EMERGENCY_DEVICE_NO`
  已在 `web.environment` 中，`app/slots.py`、`app/publish.py`、`templates/publish.html`
  都会被现有 `COPY app ./app` 带入镜像，`scripts/slot_check.py` 随 `COPY scripts` 带入。
  请在目标机执行：
  ```bash
  docker compose up --build
  python3 scripts/slot_check.py  --base-url http://localhost:8000
  python3 scripts/e2e_check.py   --base-url http://localhost:8000 --crash-flow
  ```
  并人工点一次 `/publish` 的一键剧本、`docker compose restart web` 验证预备态名额不丢、
  以及「④ 预备前崩溃」后 Compose 自动拉起且席位已归还。
- 未接真实设备侧回执/工单系统；“人工核实”结论与抢修身份目前只分别登记在网页 SQLite / 由环境变量指定。
- 演示应用未加登录鉴权/HTTPS/CSRF 防护，生产化还需补访问控制、审计对接与密钥管理；
  `ENABLE_CRASH_DEMO` 生产环境应保持 `false`。
- 总名额目前通过环境变量在启动时固定（改动需重启网页；席位本体在 PG，重启不影响占用）。
  若需要“不重启在线调额”，可再加一张配置表与热加载，但要处理缩容时已占高编号席的迁移/等待，
  本次按“可在 PG 上限内配置”用 12-factor 环境变量交付。
