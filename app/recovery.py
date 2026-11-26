"""恢复核心逻辑：数据库取证 → 状态判定 → 幂等收尾/补登记。

判定原则（宁可标“待核实”，也不臆断）：

1. ``pg_prepared_xacts`` 中仍有该 gxid
   => 事务处于 OPEN/PREPARED，锁仍保留；一切以协调端 SQLite 决定为准。
2. 预备事务已不存在，但库内存在只能由该预备事务写入的提交标记 / 修订记录
   => 强证据证明已 COMMIT PREPARED。
3. 预备事务已不存在、提交证据缺失，但发布单还在且当前值等于基线前像值
   （且基线值≠目标值、updated_gxid 不是本事务）
   => 证据相互印证，判定已 ROLLBACK PREPARED。
4. 其余情况（发布单缺失、基线已被后续发布覆盖、值对不上……）
   => 证据不足，标记 UNVERIFIED（待核实），由值班员人工核查登记。
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import psycopg2

from . import config, sqlite_store
from .pgdb import pg_conn


class RecoveryError(Exception):
    """可向值班员展示的业务错误。"""


# 状态码 -> 中文文案 / 语义色调 / 操作提示
STATE_META: dict[str, dict[str, str]] = {
    "NOT_FOUND": {
        "label": "查无此事务",
        "tone": "muted",
        "advice": "PostgreSQL、协调决定库和网页登记簿中都没有这个号，请核对号码。",
    },
    "OPEN_AWAITING": {
        "label": "挂起中 · 待确认决定",
        "tone": "warn",
        "advice": "预备事务仍在数据库中，行锁/表锁继续保留。协调端没有留下决定前不得擅自收尾；请联系负责人确认后在下方人工补录决定。",
    },
    "OPEN_COMMIT": {
        "label": "挂起中 · 决定提交",
        "tone": "info",
        "advice": "协调端已在 SQLite 留下 COMMIT 决定，可执行 COMMIT PREPARED 完成发布。",
    },
    "OPEN_ABORT": {
        "label": "挂起中 · 决定撤销",
        "tone": "info",
        "advice": "协调端已在 SQLite 留下 ABORT 决定，可执行 ROLLBACK PREPARED 撤销发布，参数保持基线值。",
    },
    "RECONCILE_COMMIT": {
        "label": "库端已提交 · 网页未登记",
        "tone": "ok",
        "advice": "重启后依据库内提交标记/修订证据判定为已提交。只需补登记，不会再次修改参数。",
    },
    "RECONCILE_ABORT": {
        "label": "库端已回滚 · 网页未登记",
        "tone": "ok",
        "advice": "重启后依据“基线前像值仍在、提交证据缺失”判定为已回滚。只需补登记，不会再次修改参数。",
    },
    "DONE_COMMITTED": {
        "label": "已收尾 · 已提交",
        "tone": "ok",
        "advice": "网页登记簿已有提交结论。重复办理会被幂等忽略，参数不会被再次修改。",
    },
    "DONE_ROLLED_BACK": {
        "label": "已收尾 · 已回滚",
        "tone": "ok",
        "advice": "网页登记簿已有回滚结论。重复办理会被幂等忽略，参数不会被再次修改。",
    },
    "DONE_CONFLICT": {
        "label": "冲突已登记",
        "tone": "danger",
        "advice": "决定与库端实际结果的冲突已登记在案，重复提交不会改变任何数据。",
    },
    "DONE_MANUAL": {
        "label": "人工核实已登记",
        "tone": "warn",
        "advice": "值班员的人工核实结论已登记，重复办理不会改变任何数据。",
    },
    "CONFLICT_COMMIT_BUT_ABORTED": {
        "label": "冲突：决定提交，实际已回滚",
        "tone": "danger",
        "advice": "SQLite 决定是 COMMIT，但数据库证据表明该预备事务最终被 ROLLBACK PREPARED。参数未发布成目标值。请指出并登记冲突，再走人工补偿流程。",
    },
    "CONFLICT_ABORT_BUT_COMMITTED": {
        "label": "冲突：决定撤销，实际已提交",
        "tone": "danger",
        "advice": "SQLite 决定是 ABORT，但数据库中已出现该事务的提交标记/修订记录，参数事实上已发布。请指出并登记冲突，再走人工补偿流程。",
    },
    "UNVERIFIED": {
        "label": "待核实 · 数据库证据不足",
        "tone": "warn",
        "advice": "预备事务已不存在，但缺少足够证据区分提交与回滚（常见于基线值又被后续业务覆盖）。不得臆断，请人工核查后登记结论。",
    },
}


def validate_gxid(gxid: str) -> str:
    gxid = (gxid or "").strip()
    if not re.match(config.GXID_RE, gxid):
        raise RecoveryError("事务号只允许 3-190 位字母、数字、下划线或连字符。")
    return gxid


def _quote_gid(gxid: str) -> str:
    """gxid 通过严格白名单校验后才能拼进工具语句（PREPARE/COMMIT/ROLLBACK 不支持参数绑定）。"""
    return "'" + gxid + "'"


def _dt(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value)


# ------------------------------------------------------------------ PG 取证

def fetch_pg_evidence(gxid: str) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "prepared": None,
        "locks": [],
        "blocked_waiters": 0,
        "order": None,
        "param": None,
        "marker": None,
        "revision": None,
        "pg_known_gxids": [],
    }
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT gid, owner, database, prepared, transaction AS xid
                     FROM pg_prepared_xacts WHERE gid = %s""",
                (gxid,),
            )
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                p = dict(zip(cols, row))
                p["prepared"] = _dt(p["prepared"])
                p["xid"] = int(p["xid"]) if p["xid"] is not None else None
                ev["prepared"] = p

                # 该预备事务持有的锁：先用 transactionid 锁找到它的 virtualtransaction，
                # 再列出同一 vxid 持有的全部锁（PG 中预备事务锁的 pid 为 NULL）。
                cur.execute(
                    """
                    WITH myvx AS (
                        SELECT l.virtualtransaction
                          FROM pg_locks l
                          JOIN pg_prepared_xacts p ON l.transactionid = p.transaction
                         WHERE p.gid = %s AND l.locktype = 'transactionid' AND l.granted
                    )
                    SELECT l.locktype,
                           COALESCE(l.relation::regclass::text, '') AS relation,
                           l.mode, l.granted, l.fastpath
                      FROM pg_locks l JOIN myvx ON l.virtualtransaction = myvx.virtualtransaction
                     ORDER BY l.locktype, relation, l.mode
                    """,
                    (gxid,),
                )
                lock_cols = [d[0] for d in cur.description]
                ev["locks"] = [dict(zip(lock_cols, r)) for r in cur.fetchall()]

                if p["xid"] is not None:
                    cur.execute(
                        "SELECT count(*) FROM pg_locks WHERE NOT granted AND transactionid = %s",
                        (p["xid"],),
                    )
                    ev["blocked_waiters"] = cur.fetchone()[0]

            cur.execute(
                "SELECT gxid, param_no, intended_value, baseline_value, reason, created_at"
                "  FROM publish_order WHERE gxid = %s",
                (gxid,),
            )
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                o = dict(zip(cols, row))
                o["created_at"] = _dt(o["created_at"])
                ev["order"] = o

                cur.execute(
                    "SELECT param_no, device_no, param_name, param_value, unit, version, updated_gxid, updated_at"
                    "  FROM calibration_param WHERE param_no = %s",
                    (o["param_no"],),
                )
                prow = cur.fetchone()
                if prow:
                    pcols = [d[0] for d in cur.description]
                    param = dict(zip(pcols, prow))
                    param["updated_at"] = _dt(param["updated_at"])
                    ev["param"] = param

            cur.execute("SELECT gxid, committed_at FROM txn_commit_marker WHERE gxid = %s", (gxid,))
            mrow = cur.fetchone()
            if mrow:
                ev["marker"] = {"gxid": mrow[0], "committed_at": _dt(mrow[1])}

            cur.execute(
                "SELECT gxid, param_no, param_value, created_at FROM param_revision WHERE gxid = %s",
                (gxid,),
            )
            rrow = cur.fetchone()
            if rrow:
                ev["revision"] = {
                    "gxid": rrow[0],
                    "param_no": rrow[1],
                    "param_value": rrow[2],
                    "created_at": _dt(rrow[3]),
                }

            cur.execute(
                """
                SELECT gid FROM pg_prepared_xacts WHERE gid = %s
                UNION SELECT gxid FROM publish_order WHERE gxid = %s
                UNION SELECT gxid FROM txn_commit_marker WHERE gxid = %s
                UNION SELECT gxid FROM param_revision WHERE gxid = %s
                """,
                (gxid, gxid, gxid, gxid),
            )
            ev["pg_known_gxids"] = [r[0] for r in cur.fetchall()]
    return ev


def classify(ev: dict[str, Any], decision: dict[str, Any] | None, record: dict[str, Any] | None) -> dict[str, Any]:
    """返回 actual（库端事实）与 state（页面处置状态）以及证据说明列表。"""
    evidence_lines: list[dict[str, str]] = []

    prepared = ev.get("prepared")
    marker = ev.get("marker")
    revision = ev.get("revision")
    order = ev.get("order")
    param = ev.get("param")

    if prepared:
        actual = "OPEN"
        evidence_lines.append({"tone": "warn", "text": f"pg_prepared_xacts 仍有该事务（owner={prepared['owner']}，prepared={prepared['prepared']}），锁继续保留。"})
    else:
        evidence_lines.append({"tone": "muted", "text": "pg_prepared_xacts 中已无该 gxid，说明 PREPARE 阶段已经结束（被 COMMIT 或 ROLLBACK PREPARED）。"})

    if marker:
        evidence_lines.append({"tone": "ok", "text": f"存在 txn_commit_marker 提交标记（{marker['committed_at']}），该行由预备事务写入，是已提交的强证据。"})
    if revision:
        evidence_lines.append({"tone": "ok", "text": f"存在 param_revision 修订记录：{revision['param_no']}={revision['param_value']}（{revision['created_at']}），同样证明该事务已提交。"})

    if prepared:
        actual = "OPEN"
    elif marker or revision:
        actual = "COMMITTED"
    elif order:
        baseline = order["baseline_value"]
        intended = order["intended_value"]
        if param is None:
            actual = "UNVERIFIED"
            evidence_lines.append({"tone": "warn", "text": "发布单引用的参数行不存在，无法比对前像值。"})
        else:
            current = param["param_value"]
            same_gxid = param.get("updated_gxid") == order["gxid"]
            evidence_lines.append({"tone": "muted", "text": f"发布单前像值={baseline}，目标值={intended}，当前库值={current}，updated_gxid={param.get('updated_gxid') or 'NULL'}。"})
            if baseline != intended and current == baseline and not same_gxid:
                actual = "ROLLED_BACK"
                evidence_lines.append({"tone": "ok", "text": "当前值等于基线前像值、且 updated_gxid 已不是本事务，与“回滚预备事务”的结果一致。"})
            else:
                actual = "UNVERIFIED"
                if current != baseline:
                    evidence_lines.append({"tone": "warn", "text": "当前值已偏离基线前像值（可能被后续发布/人工修改覆盖），回滚前像证据失效；又缺少提交标记，无法仅凭数据库区分提交与回滚。"})
                elif baseline == intended:
                    evidence_lines.append({"tone": "warn", "text": "基线值与目标值相同，值比对没有区分度，且缺少提交标记。"})
                elif same_gxid:
                    evidence_lines.append({"tone": "warn", "text": "updated_gxid 仍指向本事务，但缺少提交标记/修订记录，证据相互矛盾。"})
                else:
                    evidence_lines.append({"tone": "warn", "text": "证据不足，无法区分提交与回滚。"})
    else:
        actual = "UNVERIFIED"
        evidence_lines.append({"tone": "warn", "text": "没有 publish_order 发布单，缺少基线值与目标值，无法做前像比对。"})

    # 完全查无此号？
    known_anywhere = bool(
        prepared or marker or revision or order or ev.get("pg_known_gxids") or decision or record
    )

    if not known_anywhere:
        state = "NOT_FOUND"
    elif record:
        handling = record.get("handling", "")
        if handling == "MANUAL_VERIFY":
            state = "DONE_MANUAL"
        elif handling == "CONFLICT_ACK":
            state = "DONE_CONFLICT"
        else:
            state = {
                "COMMITTED": "DONE_COMMITTED",
                "ROLLED_BACK": "DONE_ROLLED_BACK",
                "CONFLICT": "DONE_CONFLICT",
                "MANUAL": "DONE_MANUAL",
            }.get(record.get("outcome", ""), "DONE_MANUAL")
    elif actual == "OPEN":
        if not decision:
            state = "OPEN_AWAITING"
        elif decision["decision"] == "COMMIT":
            state = "OPEN_COMMIT"
        else:
            state = "OPEN_ABORT"
    elif actual == "COMMITTED":
        if decision and decision["decision"] == "ABORT":
            state = "CONFLICT_ABORT_BUT_COMMITTED"
        else:
            state = "RECONCILE_COMMIT"
    elif actual == "ROLLED_BACK":
        if decision and decision["decision"] == "COMMIT":
            state = "CONFLICT_COMMIT_BUT_ABORTED"
        else:
            state = "RECONCILE_ABORT"
    else:
        state = "UNVERIFIED"

    meta = STATE_META[state]
    return {
        "actual": actual,
        "state": state,
        "state_label": meta["label"],
        "tone": meta["tone"],
        "advice": meta["advice"],
        "evidence_lines": evidence_lines,
        "known_anywhere": known_anywhere,
    }


def inspect(gxid: str) -> dict[str, Any]:
    gxid = validate_gxid(gxid)
    decision = sqlite_store.get_decision(gxid)
    record = sqlite_store.get_record(gxid)
    events = sqlite_store.list_events(gxid)
    ev = fetch_pg_evidence(gxid)
    verdict = classify(ev, decision, record)
    return {
        "gxid": gxid,
        **verdict,
        "decision": decision,
        "record": record,
        "events": events,
        **ev,
    }


# ------------------------------------------------------------------ 列表/查询

def list_params() -> list[dict[str, Any]]:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.param_no, p.device_no, p.param_name, p.param_value, p.unit,
                       p.version, p.updated_gxid, p.updated_at,
                       (SELECT count(*) FROM publish_order o WHERE o.param_no = p.param_no) AS order_count,
                       (SELECT count(*) FROM pg_prepared_xacts x
                          JOIN publish_order o ON o.gxid = x.gid
                         WHERE o.param_no = p.param_no) AS open_count
                  FROM calibration_param p
                 ORDER BY p.param_no
                """
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["updated_at"] = _dt(r["updated_at"])
    return rows


def get_param(param_no: str) -> dict[str, Any] | None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT param_no, device_no, param_name, param_value, unit, version,
                          updated_gxid, updated_at
                     FROM calibration_param WHERE param_no = %s""",
                (param_no,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            param = dict(zip(cols, row))
            param["updated_at"] = _dt(param["updated_at"])

            cur.execute(
                """SELECT o.gxid, o.intended_value, o.baseline_value, o.created_at,
                          EXISTS(SELECT 1 FROM pg_prepared_xacts x WHERE x.gid = o.gxid) AS prepared
                     FROM publish_order o WHERE o.param_no = %s
                    ORDER BY o.created_at DESC""",
                (param_no,),
            )
            ocols = [d[0] for d in cur.description]
            orders = []
            for r in cur.fetchall():
                o = dict(zip(ocols, r))
                o["created_at"] = _dt(o["created_at"])
                orders.append(o)
    param["orders"] = orders
    return param


def search(q: str) -> dict[str, list[dict[str, Any]]]:
    q = (q or "").strip()
    like = f"%{q}%"
    params: list[dict[str, Any]] = []
    gxids: list[str] = []
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT param_no, device_no, param_name, param_value, unit, version
                     FROM calibration_param
                    WHERE param_no ILIKE %s OR device_no ILIKE %s OR param_name ILIKE %s
                    ORDER BY param_no LIMIT 50""",
                (like, like, like),
            )
            cols = [d[0] for d in cur.description]
            params = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.execute(
                """SELECT DISTINCT gid FROM pg_prepared_xacts WHERE gid ILIKE %s
                   UNION SELECT DISTINCT gxid FROM publish_order WHERE gxid ILIKE %s
                   UNION SELECT DISTINCT gxid FROM param_revision WHERE gxid ILIKE %s""",
                (like, like, like),
            )
            gxids = [r[0] for r in cur.fetchall()]

    for d in sqlite_store.list_decisions():
        if q.lower() in d["gxid"].lower() and d["gxid"] not in gxids:
            gxids.append(d["gxid"])
    for r in sqlite_store.list_records():
        if q.lower() in r["gxid"].lower() and r["gxid"] not in gxids:
            gxids.append(r["gxid"])

    txns = []
    for g in gxids[:50]:
        try:
            txns.append(inspect(g))
        except RecoveryError:
            continue
    return {"params": params, "txns": txns}


def list_transactions() -> list[dict[str, Any]]:
    gxids: set[str] = set()
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT gid FROM pg_prepared_xacts")
            gxids.update(r[0] for r in cur.fetchall())
            cur.execute("SELECT gxid FROM publish_order")
            gxids.update(r[0] for r in cur.fetchall())
    gxids.update(d["gxid"] for d in sqlite_store.list_decisions())
    gxids.update(r["gxid"] for r in sqlite_store.list_records())

    items = [inspect(g) for g in gxids]
    # 未完结的排前面，再按创建时间/事务号倒序
    done_prefix = ("DONE_",)
    items.sort(key=lambda s: (s["state"].startswith(done_prefix), s.get("order", {}).get("created_at") or "", s["gxid"]), reverse=False)
    items.sort(key=lambda s: s["state"].startswith(done_prefix))
    return items


# ------------------------------------------------------------------ 收尾动作

def finalize_from_decision(gxid: str, operator: str = "web", crash_after_pg: bool = False) -> dict[str, Any]:
    """按 SQLite 中的协调决定执行 COMMIT/ROLLBACK PREPARED，并幂等登记。

    重复/并发调用：在 SQLite 写事务中串行检查登记簿；已有记录则原样返回，
    不再触碰参数。recovery_record 主键是最终兜底。
    """
    gxid = validate_gxid(gxid)

    with sqlite_store.immediate_web_tx() as wconn:
        existing = sqlite_store.get_record_on_conn(wconn, gxid)
        if existing is not None:
            return {"idempotent": True, "record": existing, "state": inspect(gxid)["state"]}

        snap = inspect(gxid)
        if snap["actual"] != "OPEN":
            raise RecoveryError("数据库中已无该预备事务，不能再执行 PREPARED 收尾；请按证据选择补登记/冲突/待核实流程。")

        decision = snap["decision"]
        if not decision:
            raise RecoveryError("协调端尚无提交/撤销决定：按规程保留预备事务及其锁，状态保持“待确认”。")

        choice = decision["decision"]
        sql = "COMMIT PREPARED" if choice == "COMMIT" else "ROLLBACK PREPARED"

        # 独立 PG 连接执行工具语句；允许“gid 已被其他途径收尾”的情况，随后用证据判定。
        with pg_conn(autocommit=True) as pg:
            cur = pg.cursor()
            try:
                cur.execute(f"{sql} {_quote_gid(gxid)}")
            except psycopg2.errors.UndefinedObject:
                pass

        if crash_after_pg:
            # 仅限演示：模拟“数据库已收尾、网页还没登记就退出”。
            # 此刻 SQLite 写事务尚未提交，进程直接结束后不会留下登记簿记录。
            os._exit(42)

        after = inspect(gxid)
        expected_actual = "COMMITTED" if choice == "COMMIT" else "ROLLED_BACK"
        if after["actual"] != expected_actual:
            sqlite_store.append_event(
                wconn, gxid, "UNEXPECTED_EVIDENCE",
                f"执行 {sql} 后取证结果为 {after['actual']}，与预期 {expected_actual} 不符，未自动登记",
                operator,
            )
            raise RecoveryError(f"已执行 {sql}，但数据库证据判定为 {after['actual']}，与决定不一致或证据不足，请转人工核查。")

        detail = f"按协调端 {choice} 决定执行 {sql}"
        sqlite_store.insert_record_on_conn(
            wconn, gxid, outcome=expected_actual, handling="FINALIZED",
            detail=detail, operator=operator,
        )
        sqlite_store.append_event(wconn, gxid, "PG_FINALIZED", detail, operator)
        sqlite_store.append_event(wconn, gxid, "FINALIZE_DONE", "收尾完成并登记", operator)

    record = sqlite_store.get_record(gxid)
    return {"idempotent": False, "record": record, "state": inspect(gxid)["state"]}


def register_from_evidence(gxid: str, operator: str = "web") -> dict[str, Any]:
    """库端事实上已经收尾、只是网页没登记：根据数据库证据补登记，绝不重新改参数。"""
    gxid = validate_gxid(gxid)
    snap = inspect(gxid)
    if snap["record"]:
        return {"idempotent": True, "record": snap["record"], "state": snap["state"]}

    if snap["state"] == "RECONCILE_COMMIT":
        outcome, detail = "COMMITTED", "重启后依据提交标记/修订记录补登记"
    elif snap["state"] == "RECONCILE_ABORT":
        outcome, detail = "ROLLED_BACK", "重启后依据基线前像值完整等证据补登记"
    else:
        raise RecoveryError(f"当前状态为“{snap['state_label']}”，不能按普通已确定结果补登记。")

    status, record = sqlite_store.save_record_once(
        gxid, outcome=outcome, handling="RECONCILED", detail=detail,
        operator=operator, event="RECONCILE_DONE",
    )
    return {"idempotent": status == "exists", "record": record, "state": inspect(gxid)["state"]}


def ack_conflict(gxid: str, operator: str, note: str) -> dict[str, Any]:
    gxid = validate_gxid(gxid)
    snap = inspect(gxid)
    if snap["record"]:
        return {"idempotent": True, "record": snap["record"], "state": snap["state"]}
    if not snap["state"].startswith("CONFLICT_"):
        raise RecoveryError("当前不是决定与事实冲突的状态，无需冲突登记。")
    detail = f"{snap['state_label']}。值班员意见：{note or '（无补充）'}"
    status, record = sqlite_store.save_record_once(
        gxid, outcome="CONFLICT", handling="CONFLICT_ACK", detail=detail,
        operator=operator or "值班员", event="CONFLICT_ACKED",
    )
    return {"idempotent": status == "exists", "record": record, "state": inspect(gxid)["state"]}


def manual_verify(gxid: str, claimed: str, operator: str, note: str) -> dict[str, Any]:
    gxid = validate_gxid(gxid)
    if claimed not in ("COMMITTED", "ROLLED_BACK"):
        raise RecoveryError("人工核实结论必须是 COMMITTED 或 ROLLED_BACK。")
    snap = inspect(gxid)
    if snap["record"]:
        return {"idempotent": True, "record": snap["record"], "state": snap["state"]}
    if snap["state"] != "UNVERIFIED":
        raise RecoveryError("只有“待核实（证据不足）”状态需要人工核实登记。")
    detail = f"人工核实库端实际为 {claimed}。说明：{note or '（无）'}"
    status, record = sqlite_store.save_record_once(
        gxid, outcome=claimed, handling="MANUAL_VERIFY", detail=detail,
        operator=operator or "值班员", event="MANUAL_VERIFIED",
    )
    return {"idempotent": status == "exists", "record": record, "state": inspect(gxid)["state"]}


def manual_decision_and_finalize(gxid: str, choice: str, operator: str, note: str, crash_after_pg: bool = False) -> dict[str, Any]:
    """决定缺失时，由值班员确认后人工补录决定，再按该决定收尾。"""
    gxid = validate_gxid(gxid)
    if choice not in ("COMMIT", "ABORT"):
        raise RecoveryError("人工决定必须是 COMMIT 或 ABORT。")
    operator = (operator or "").strip() or "值班员"

    snap = inspect(gxid)
    if snap["actual"] != "OPEN":
        raise RecoveryError("预备事务已不在库中，不能补录决定并收尾；请按当前证据状态办理。")

    existing = snap["decision"]
    if existing is None:
        sqlite_store.write_decision(
            gxid, choice,
            reason=f"值班员人工确认后补录：{note or '（无说明）'}",
            decided_by=f"manual:{operator}",
        )
        sqlite_store.add_event(gxid, "MANUAL_DECISION", f"值班员补录 {choice} 决定：{note}", operator)
    # 若协调决定其实已出现，坚持按既有不可变决定执行。
    return finalize_from_decision(gxid, operator=operator, crash_after_pg=crash_after_pg)


# ------------------------------------------------------------------ 锁阻塞演示

def probe_row_lock(param_no: str, timeout_ms: int | None = None) -> dict[str, Any]:
    """用另一个会话尝试更新同一行，证明预备事务的锁仍在。

    正常环境下 PostgreSQL 的 lock_timeout 会按时返回 55P03；
    若服务端定时器/信号异常，客户端守护线程会在硬超时后断开测试连接，
    保证网页请求不会被无限挂住。任何情况下都回滚，不修改参数。
    """
    timeout_ms = timeout_ms or config.LOCK_DEMO_TIMEOUT_MS
    holder: dict[str, psycopg2.extensions.connection] = {}
    outcome: dict[str, Any] = {"acquired": None, "sqlstate": None, "waited_ms": None, "note": ""}

    def worker() -> None:
        conn = None
        try:
            conn = psycopg2.connect(
                config.DATABASE_URL, connect_timeout=5,
                options=f"-c lock_timeout={timeout_ms}",
                application_name="lock-demo",
            )
            holder["conn"] = conn
            cur = conn.cursor()
            start = time.monotonic()
            cur.execute(
                "UPDATE calibration_param SET param_value = param_value WHERE param_no = %s",
                (param_no,),
            )
            conn.rollback()  # 明确不改数据
            outcome["acquired"] = True
            outcome["waited_ms"] = int((time.monotonic() - start) * 1000)
        except psycopg2.errors.LockNotAvailable as exc:
            outcome["acquired"] = False
            outcome["sqlstate"] = exc.sqlstate
        except Exception as exc:  # 客户端硬断开时会走到这里
            outcome["acquired"] = False
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    t = threading.Thread(target=worker, daemon=True)
    start = time.monotonic()
    t.start()
    cap_seconds = timeout_ms / 1000.0 + 2.5
    t.join(cap_seconds)

    if t.is_alive():
        conn = holder.get("conn")
        if conn is not None:
            try:
                os.shutdown(conn.fileno(), os.SHUT_RDWR)
            except Exception:
                pass
        t.join(2.0)
        outcome["acquired"] = False
        outcome["client_timeout"] = True
        outcome["waited_ms"] = int((time.monotonic() - start) * 1000)
        outcome["note"] = "服务端 lock_timeout 未按时生效，已由网页硬超时断开测试会话；阻塞事实以 pg_locks 为准。"
    elif outcome.get("waited_ms") is None:
        outcome["waited_ms"] = int((time.monotonic() - start) * 1000)

    if outcome["acquired"] is False and outcome.get("sqlstate") == "55P03":
        outcome["note"] = f"{timeout_ms}ms 内拿不到行锁（PostgreSQL 55P03 lock_not_available），说明预备事务仍持锁，其他发布被挡住。"
    elif outcome["acquired"] is True:
        outcome["note"] = "更新立即拿到了行锁：该参数当前没有被预备事务锁定。"
    return outcome
