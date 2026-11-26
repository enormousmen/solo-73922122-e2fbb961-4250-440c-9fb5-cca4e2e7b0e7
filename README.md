# 设备校准参数发布 · 预备事务恢复台（FastAPI + PostgreSQL + SQLite）

设备校准参数通过 2PC（两阶段提交）发布，协调端与参与者之间可能在任意时刻中断。
本项目提供一个值班恢复网页：按**参数号 / 设备号 / 事务号**查询，依据 **SQLite 中协调端留下的
COMMIT/ABORT 决定**收尾 PostgreSQL 的预备事务；当网页自己还没登记就退出、重启后，则完全依据
**数据库证据**区分该事务最终是已提交还是已回滚。

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
docker-compose.yml          db(postgres:16, max_prepared_transactions=100) + web
Dockerfile                  python:3.12-slim + uvicorn 单 worker
db/01_schema.sql            表结构 + 3 条内置校准参数样例（容器首启自动执行；应用启动也幂等执行）
app/
  main.py                   FastAPI 页面/表单/JSON 路由
  config.py                 环境变量配置
  pgdb.py                   PG 连接、等待就绪、幂等建表
  sqlite_store.py           协调决定库 + 网页登记簿/审计事件
  recovery.py               取证、状态分类、收尾/补登记/冲突/人工核实、锁演示
  sample.py                 内置参数发布流程 + 8 种中断现场构造
  coord_cli.py              协调端命令行：向 SQLite 写 COMMIT/ABORT 决定
  templates/ static/        服务端渲染页面
scripts/e2e_check.py        纯标准库 HTTP 端到端核对（含可选崩溃/重启流程）
tests/test_recovery.py      pytest + TestClient，12 个关键场景
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

## 5. 交付核对：已完成 / 已实测 / 未完成

### 已完成（代码与交付物）

- FastAPI 恢复页 + JSON 接口 + 服务端渲染中文 UI；Dockerfile、docker-compose.yml、初始化 SQL、`.env.example`。
- PostgreSQL 2PC 全流程样例：发布单（前像/目标值）、预备事务、参数修订表、提交标记表；3 条内置校准参数。
- SQLite 协调决定库（不可变）与网页登记簿/审计事件流。
- 按参数号/设备号/事务号查询；参数详情与发布历史；预备事务锁清单与“他人修改被阻塞”演示。
- 8 种一键中断现场：有决定提交/回滚、无决定待确认、库端已提交/已回滚但未登记、两种决定-事实冲突、证据不足待核实。
- 收尾、证据补登记、冲突登记、人工核实、无决定时人工补录并收尾；全链路幂等与并发串行化。
- 崩溃演示开关（`os._exit(42)`，恰在 PG 收尾后、SQLite 登记前），配合 Compose `restart` 演示重启恢复。
- `scripts/e2e_check.py` 与 `tests/test_recovery.py`。

### 已在本环境实际试跑

本环境**没有 Docker 守护进程/CLI**，因此用 PyPI 上的 `pgserver` 内置 PostgreSQL 16.2
（与 `postgres:16` 大版本一致）+ 本机 uvicorn 做了等价实测：

- `python3 -m pytest -v`：**12 passed**（每次用一次性全新 PG 实例复跑亦通过）。
  覆盖：全部页面渲染、无决定保留事务与锁并拒绝收尾、人工提交/回滚、按决定收尾、
  重复办理参数不变、6 线程并发收尾恰好 1 次生效、提交/回滚未登记的证据补登记、
  两种冲突、待核实与人工结论、锁探测不改数据。
- 真实 HTTP 端到端 `scripts/e2e_check.py`（对本机 uvicorn，不含 `--crash-flow` 轮询）：
  **49 项断言全部通过**（8 种现场文案、所有表单动作与 PRG 跳转、幂等提示、搜索、锁演示 303）。
  带 `--crash-flow` 实跑时，“构造崩溃样例 + 收尾请求确实触发 `os._exit(42)`/连接中断”通过，
  但本沙箱里守护脚本第二次拉起的 uvicorn 卡在启动期（沙箱跨进程信号受限、早期失控探测进程堆积所致，
  见下条环境说明），因此“轮询等同一个 HTTP 服务复活”的 4 项在本机未通过；这不是应用判定逻辑问题。
- 崩溃/重启数据正确性专项（`/tmp` 下一次性脚本：真实子进程执行 `os._exit(42)`，再用**全新解释器**
  等价“重启后的网页”取证）：**ALL_OK**——崩溃后 `recovery_record` 为空；COMMIT 路线重启后判定
  `COMMITTED / RECONCILE_COMMIT`，ABORT 路线判定 `ROLLED_BACK / RECONCILE_ABORT`；
  新进程补登记不改参数，重复补登记返回幂等。Docker 下 `--crash-flow` 的服务复活由
  `restart: unless-stopped` 完成，预期可全绿，但本机无 Docker 未实跑。
- 协调 CLI 的写入/列表/决定不可变也已实测。
- 说明：本沙箱会限制 PostgreSQL 跨进程信号，导致服务端 `lock_timeout` 与 advisory lock
  唤醒在“跨 Shell 调用遗留”的实例上偶发失效（普通 Docker/Linux 不存在该限制）。
  因此代码把并发串行化放在 SQLite 写事务上，并给锁演示加了客户端硬超时；
  锁阻塞事实通过 `pg_locks/pg_stat_activity` 与 pytest 的 `55P03/客户端超时` 双路径验证。

### 未完成 / 需在有 Docker 的机器上补验

- **未在本机构建/启动 `docker compose`**（环境无 `docker` 可执行文件与守护进程）。
  Compose/YAML 已做语法解析，镜像依赖均为常规 wheel；请在目标机执行
  `docker compose up --build` 与 `python3 scripts/e2e_check.py --base-url http://localhost:8000 --crash-flow` 做最终确认。
- 未接真实设备侧回执/工单系统；“人工核实”结论目前只登记在网页 SQLite。
- 演示应用未加登录鉴权/HTTPS/CSRF 防护，生产化还需补访问控制、审计对接与密钥管理；
  `ENABLE_CRASH_DEMO` 生产环境应保持 `false`。
