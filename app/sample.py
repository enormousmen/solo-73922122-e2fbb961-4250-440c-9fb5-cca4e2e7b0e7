"""内置“设备校准参数发布”样例与各类中断现场构造。

一次标准 2PC 发布：
  1. 在普通事务里写 publish_order（前像/目标值），独立提交，保证回滚后仍可取证；
  2. 另开事务 UPDATE calibration_param、写 param_revision、写 txn_commit_marker；
  3. PREPARE TRANSACTION 'GX-...' —— 此后参数改动对其他会话不可见，但锁一直保留；
  4. 协调端把 COMMIT/ABORT 决定写入 SQLite（真实系统中这是协调器的决定日志）；
  5. 由恢复网页执行 COMMIT/ROLLBACK PREPARED 并登记。

下方各种 scenario 对应“发布到一半断掉”的不同时刻。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import sqlite_store
from .pgdb import pg_conn

SCENARIOS: dict[str, str] = {
    "open_commit": "挂起①：已 PREPARE，协调端已留 COMMIT 决定（等网页提交收尾）",
    "open_abort": "挂起②：已 PREPARE，协调端已留 ABORT 决定（等网页回滚收尾）",
    "open_no_decision": "挂起③：已 PREPARE，决定尚未写入即中断（保留事务与锁，待确认）",
    "committed_unregistered": "重启④：库端已 COMMIT PREPARED，但网页没登记就退出",
    "rolledback_unregistered": "重启⑤：库端已 ROLLBACK PREPARED，但网页没登记就退出",
    "conflict_commit_vs_rollback": "冲突⑥：SQLite 写 COMMIT，库端实际却被回滚",
    "conflict_abort_vs_commit": "冲突⑦：SQLite 写 ABORT，库端实际却已提交",
    "unverified_overwrite": "待核实⑧：已回滚，但基线值随后被新校准覆盖，证据不足",
}


def new_gxid() -> str:
    return "GX-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]


def create_demo_param() -> str:
    """为一次演示新建独立参数，避免多个挂起事务抢同一行锁导致构造卡住。"""
    suffix = uuid.uuid4().hex[:6].upper()
    param_no = f"CAL-DEMO-{suffix}"
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO calibration_param
                       (param_no, device_no, param_name, param_value, unit, version, updated_gxid)
                   VALUES (%s, %s, '演示校准参数（可随时重置）', '0.1000', 'ms', 1, NULL)""",
                (param_no, f"DEV-D-{suffix[:4]}"),
            )
    return param_no


def _current_value(param_no: str) -> tuple[str, str | None]:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT param_value, updated_gxid FROM calibration_param WHERE param_no=%s", (param_no,))
            value, gxid = cur.fetchone()
    return value, gxid


def _bump(value: str) -> str:
    try:
        return f"{float(value) + 0.0137: .5f}".strip()
    except ValueError:
        return value + "-R2"


def _prepare_publish(gxid: str, param_no: str | None) -> tuple[str, str, str]:
    """写发布单 + 预备事务。返回 (param_no, baseline, intended)。"""
    if param_no is None:
        param_no = create_demo_param()

    baseline, _ = _current_value(param_no)
    intended = _bump(baseline)
    if intended == baseline:  # 极端情况下保证前像/目标可区分
        intended = baseline + ".1"

    # 1) 发布单独立落盘，回滚不消失
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO publish_order(gxid, param_no, intended_value, baseline_value, reason)
                   VALUES (%s,%s,%s,%s,'内置参数发布样例')
                   ON CONFLICT (gxid) DO NOTHING""",
                (gxid, param_no, intended, baseline),
            )

    # 2) 预备事务：参数改动 + 两类提交证据，全部在同一事务内。
    #    autocommit + 显式 BEGIN：PREPARE TRANSACTION 结束事务后连接处于干净状态，
    #    close 时不会再发 ROLLBACK/COMMIT 产生 “there is no transaction in progress” 警告。
    conn = psycopg2_connect_for_prepare()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute(
            """UPDATE calibration_param
                  SET param_value=%s, version=version+1, updated_gxid=%s, updated_at=now()
                WHERE param_no=%s""",
            (intended, gxid, param_no),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"参数 {param_no} 不存在，无法发布")
        cur.execute(
            "INSERT INTO param_revision(gxid, param_no, param_value) VALUES (%s,%s,%s)",
            (gxid, param_no, intended),
        )
        cur.execute("INSERT INTO txn_commit_marker(gxid) VALUES (%s)", (gxid,))
        # gxid 由本模块生成（白名单字符），可以安全拼接
        cur.execute(f"PREPARE TRANSACTION '{gxid}'")
    finally:
        conn.close()

    return param_no, baseline, intended


def psycopg2_connect_for_prepare():
    import psycopg2

    from . import config

    conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=5)
    conn.autocommit = True
    return conn


def _pg_exec_autocommit(sql: str, gxid: str | None = None, params: tuple | None = None) -> None:
    with pg_conn(autocommit=True) as conn:
        with conn.cursor() as cur:
            if params is not None:
                cur.execute(sql, params)
            else:
                cur.execute(sql)


def create_scenario(kind: str, param_no: str | None = None) -> str:
    if kind not in SCENARIOS:
        raise ValueError(f"未知场景: {kind}")

    gxid = new_gxid()
    param_no, baseline, intended = _prepare_publish(gxid, param_no)

    if kind == "open_commit":
        sqlite_store.write_decision(gxid, "COMMIT", "样例：协调器写盘提交决定后崩溃", "coordinator-demo")

    elif kind == "open_abort":
        sqlite_store.write_decision(gxid, "ABORT", "样例：协调器检测到校验失败，留下撤销决定", "coordinator-demo")

    elif kind == "open_no_decision":
        pass  # 决定写入前就断了：SQLite 中什么都没有

    elif kind == "committed_unregistered":
        sqlite_store.write_decision(gxid, "COMMIT", "样例：决定已留，网页执行提交后、登记前退出", "coordinator-demo")
        _pg_exec_autocommit(f"COMMIT PREPARED '{gxid}'")

    elif kind == "rolledback_unregistered":
        sqlite_store.write_decision(gxid, "ABORT", "样例：决定已留，网页执行回滚后、登记前退出", "coordinator-demo")
        _pg_exec_autocommit(f"ROLLBACK PREPARED '{gxid}'")

    elif kind == "conflict_commit_vs_rollback":
        sqlite_store.write_decision(gxid, "COMMIT", "样例：协调端以为应该提交", "coordinator-demo")
        _pg_exec_autocommit(f"ROLLBACK PREPARED '{gxid}'")  # 但库端被人工/启发式回滚

    elif kind == "conflict_abort_vs_commit":
        sqlite_store.write_decision(gxid, "ABORT", "样例：协调端要求撤销", "coordinator-demo")
        _pg_exec_autocommit(f"COMMIT PREPARED '{gxid}'")  # 但库端已被误提交

    elif kind == "unverified_overwrite":
        sqlite_store.write_decision(gxid, "COMMIT", "样例：旧决定为提交，但现场已被后续操作破坏", "coordinator-demo")
        _pg_exec_autocommit(f"ROLLBACK PREPARED '{gxid}'")
        # 后续另一次校准（独立事务）又改了这个参数，基线前像被覆盖
        with pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE calibration_param
                          SET param_value='9.8765', version=version+1,
                              updated_gxid='GX-LATER-RECAL', updated_at=now()
                        WHERE param_no=%s""",
                    (param_no,),
                )

    sqlite_store.add_event(gxid, "SCENARIO_CREATED", f"构造样例场景：{kind} / {SCENARIOS[kind]}", "demo")
    return gxid
