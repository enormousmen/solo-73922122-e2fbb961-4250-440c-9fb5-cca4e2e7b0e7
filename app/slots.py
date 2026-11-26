"""发布台名额（席位）管理：PostgreSQL 事务级 advisory lock 即席位。

为什么用 ``pg_advisory_xact_lock``：

- 建单受理的同一事务里申请席位，之后执行可能阻塞的参数行 ``UPDATE``，
  **等行锁期间席位一直持有**；
- 一旦 ``PREPARE TRANSACTION`` 成功，advisory lock 随预备事务转移，
  **网页进程重启也不释放**，``pg_locks`` 里仍能查到授予记录；
- 库端任何会话直接 ``COMMIT PREPARED`` / ``ROLLBACK PREPARED`` 后，
  PostgreSQL 自动释放该锁 —— **无需网页补登记，席位立即归还**；
- 预备前建单失败 / 进程中途退出：显式 rollback/close，或连接断开后后端回滚，
  席位同样立即归还。

席位编号 ``1..N``，最后一席 ``N`` 为抢修预留席，其余为普通席。
状态一律以 ``pg_locks`` / ``pg_prepared_xacts`` 为准，不依赖网页内存，
因此“网页重启后剩余名额”仍然正确。
"""
from __future__ import annotations

from typing import Any

import psycopg2

from . import config
from .pgdb import pg_conn

# 两参数 advisory lock 的第一个 int4（命名空间），第二个 int4 放席位号。
SLOT_LOCK_NAMESPACE = 75010001
APP_PREFIX = "calpub"


class SlotConfigError(Exception):
    """名额配置不合法（超出 PostgreSQL 上限等）。"""


class SlotsFullError(Exception):
    """没有可分配的席位。``code`` 给出可展示/可程序化判断的拒绝原因。"""

    def __init__(self, code: str, message: str, status: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or {}


# ------------------------------------------------------------------ 配置/布局

def total_slots() -> int:
    """当前总名额（每次调用都读 config，测试可 monkeypatch config.PUBLISH_MAX_SLOTS）。"""
    total = int(config.PUBLISH_MAX_SLOTS)
    if total < 1:
        raise SlotConfigError("PUBLISH_MAX_SLOTS 必须 >= 1（其中 1 席为抢修预留席）。")
    return total


def reserved_slot_no(total: int | None = None) -> int:
    total = total or total_slots()
    return total  # 最后一席固定为预留席


def general_slot_numbers(total: int | None = None) -> list[int]:
    total = total or total_slots()
    return list(range(1, total))  # 1 .. N-1


def is_emergency_device(device_no: str | None) -> bool:
    return bool(device_no) and device_no.strip() in config.EMERGENCY_DEVICE_NOS


def pg_limits() -> dict[str, int]:
    """读取 PostgreSQL 侧上限，作为“可在 PostgreSQL 上限内配置”的依据。"""
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW max_prepared_transactions")
            mpt = int(cur.fetchone()[0])
            cur.execute("SHOW max_connections")
            mc = int(cur.fetchone()[0])
    return {"max_prepared_transactions": mpt, "max_connections": mc}


def validate_total_against_pg(total: int | None = None) -> dict[str, Any]:
    total = total or total_slots()
    lim = pg_limits()
    problems: list[str] = []
    if lim["max_prepared_transactions"] <= 0:
        problems.append("PostgreSQL max_prepared_transactions=0，无法使用预备事务/席位跨重启保留。")
    elif total > lim["max_prepared_transactions"]:
        problems.append(
            f"总名额 {total} 超过 max_prepared_transactions={lim['max_prepared_transactions']}。"
        )
    # 每个在途席位至少占用一个连接，给网页自身连接和运维连接留余量。
    if total > lim["max_connections"] - 3:
        problems.append(
            f"总名额 {total} 超过 max_connections={lim['max_connections']} 的安全余量。"
        )
    return {"total": total, **lim, "ok": not problems, "problems": problems}


# ------------------------------------------------------------------ 申请席位

def candidate_slots(device_no: str | None, total: int | None = None) -> list[int]:
    """返回某设备的尝试顺序。

    普通设备：只尝试普通席（预留席对其不可见、不可用）。
    抢修设备：先用预留席，再用普通席兜底。
    """
    total = total or total_slots()
    reserved = reserved_slot_no(total)
    general = general_slot_numbers(total)
    if is_emergency_device(device_no):
        return [reserved, *general]
    return general


def try_acquire(conn: psycopg2.extensions.connection, device_no: str | None) -> int:
    """在调用方已开启的事务内，非阻塞地尝试候选席位。

    成功返回席位号（advisory xact lock 已在该事务内持有）；
    没有合格空席时抛 :class:`SlotsFullError`，并附带最新名额快照与明确原因码。
    """
    total = total_slots()
    candidates = candidate_slots(device_no, total)
    cur = conn.cursor()
    acquired: int | None = None
    for slot_no in candidates:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (SLOT_LOCK_NAMESPACE, slot_no))
        if cur.fetchone()[0]:
            acquired = slot_no
            break

    if acquired is not None:
        return acquired

    snap = status()
    emergency = is_emergency_device(device_no)
    if emergency:
        code = "ALL_SLOTS_FULL"
        message = (
            f"抢修发布也无法受理：发布台总名额 {snap['used']}/{snap['total']} 已满"
            f"（抢修预留席{'占用中' if snap['reserved_used'] else '空闲'}，"
            f"普通席 {snap['general_used']}/{snap['general_total']} 占用）。"
        )
    elif snap["general_total"] == 0:
        code = "NO_GENERAL_SLOT"
        message = (
            f"当前总名额只有 {total} 席且全部为抢修预留席，"
            f"仅指定抢修设备 {'/'.join(sorted(config.EMERGENCY_DEVICE_NOS)) or '（未配置）'} 可受理，"
            "普通设备暂无可用名额。"
        )
    else:
        code = "GENERAL_SLOTS_FULL"
        reserved_devices = "/".join(sorted(config.EMERGENCY_DEVICE_NOS)) or "指定抢修设备"
        message = (
            f"普通发布名额已满：普通席 {snap['general_used']}/{snap['general_total']} 全部占用"
            f"（含正在等待参数行锁、尚未 PREPARE 的发布）。"
            f"第 {snap['reserved_slot_no']} 席是抢修预留席，只对 {reserved_devices} 开放，"
            "普通建单不能使用，请稍后重试。"
        )
    raise SlotsFullError(code, message, snap)


# ------------------------------------------------------------------ 名额状态

def _occupied_lock_rows(cur) -> list[dict[str, Any]]:
    """查出本命名空间下所有“已授予”的席位锁，并映射到预备事务 gid / 活动会话。"""
    cur.execute(
        """
        SELECT l.objid::int                                   AS slot_no,
               l.pid                                          AS pid,
               l.virtualtransaction                           AS vxid,
               a.application_name                             AS application_name,
               a.state                                        AS activity_state,
               a.wait_event_type                              AS wait_event_type,
               a.wait_event                                   AS wait_event,
               (
                   SELECT p.gid
                     FROM pg_locks t
                     JOIN pg_prepared_xacts p ON p.transaction = t.transactionid
                    WHERE t.virtualtransaction = l.virtualtransaction
                      AND t.locktype = 'transactionid'
                      AND t.granted
                    LIMIT 1
               )                                              AS gid
          FROM pg_locks l
          LEFT JOIN pg_stat_activity a ON a.pid = l.pid
         WHERE l.locktype = 'advisory'
           AND l.granted
           AND l.classid::bigint = %s
        """,
        (SLOT_LOCK_NAMESPACE,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def parse_app_name(app_name: str | None) -> tuple[str | None, str | None]:
    """``calpub:<gxid>:<device>`` -> (gxid, device)。"""
    if not app_name or not app_name.startswith(APP_PREFIX + ":"):
        return None, None
    parts = app_name.split(":", 2)
    if len(parts) == 3:
        return parts[1] or None, parts[2] or None
    return None, None


def status(total: int | None = None) -> dict[str, Any]:
    """返回名额看板所需的完整快照（始终实时查 PostgreSQL）。"""
    total = total or total_slots()
    reserved_no = reserved_slot_no(total)

    with pg_conn() as conn:
        with conn.cursor() as cur:
            rows = _occupied_lock_rows(cur)
            gids = [r["gid"] for r in rows if r.get("gid")]
            order_info: dict[str, dict[str, Any]] = {}
            if gids:
                cur.execute(
                    """
                    SELECT o.gxid, o.param_no, p.device_no
                      FROM publish_order o
                      LEFT JOIN calibration_param p ON p.param_no = o.param_no
                     WHERE o.gxid = ANY(%s)
                    """,
                    (gids,),
                )
                for gxid, param_no, device_no in cur.fetchall():
                    order_info[gxid] = {"param_no": param_no, "device_no": device_no}

    by_slot = {r["slot_no"]: r for r in rows}
    slots: list[dict[str, Any]] = []
    for n in range(1, total + 1):
        kind = "reserved" if n == reserved_no else "general"
        row = by_slot.get(n)
        item: dict[str, Any] = {
            "slot_no": n,
            "kind": kind,
            "kind_label": "抢修预留席" if kind == "reserved" else "普通席",
            "occupied": row is not None,
            "phase": "free",
            "phase_label": "空闲",
            "gxid": None,
            "device_no": None,
            "param_no": None,
            "pid": None,
            "wait_event": None,
        }
        if row is not None:
            item["pid"] = row.get("pid")
            gid = row.get("gid")
            app_gxid, app_device = parse_app_name(row.get("application_name"))
            info = order_info.get(gid or "", {})
            item["gxid"] = gid or app_gxid
            item["device_no"] = info.get("device_no") or app_device
            item["param_no"] = info.get("param_no")
            if gid:
                item["phase"] = "prepared"
                item["phase_label"] = "预备态（重启仍占额，库端收尾后释放）"
            elif row.get("wait_event_type") == "Lock":
                item["phase"] = "waiting_lock"
                item["phase_label"] = "建单处理中 · 正在等参数行锁（占席）"
                item["wait_event"] = row.get("wait_event")
            elif row.get("activity_state") == "active":
                item["phase"] = "working"
                item["phase_label"] = "建单处理中（占席）"
            else:
                item["phase"] = "accepted"
                item["phase_label"] = "已受理 · 建单事务未结束（占席）"
        slots.append(item)

    general_total = total - 1
    general_used = sum(1 for s in slots if s["kind"] == "general" and s["occupied"])
    reserved_used = sum(1 for s in slots if s["kind"] == "reserved" and s["occupied"])
    used = general_used + reserved_used
    return {
        "total": total,
        "used": used,
        "remaining": total - used,
        "general_total": general_total,
        "general_used": general_used,
        "general_remaining": general_total - general_used,
        "reserved_slot_no": reserved_no,
        "reserved_total": 1,
        "reserved_used": reserved_used,
        "reserved_remaining": 1 - reserved_used,
        "emergency_devices": sorted(config.EMERGENCY_DEVICE_NOS),
        "slots": slots,
    }
