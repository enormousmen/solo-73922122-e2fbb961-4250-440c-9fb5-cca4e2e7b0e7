"""发布台下单流程 + “抢修放行”演示编排。

一笔发布的席位生命周期（与 :mod:`app.slots` 的 advisory xact lock 对应）：

    建单受理 ── try_acquire 拿席（同一 PG 事务）
      │         写 publish_order（独立事务提交，保留前像证据）
      │         UPDATE 参数行 …… 可能阻塞等行锁，但席位始终在手
      │         写 param_revision / txn_commit_marker
      └─ PREPARE TRANSACTION ── 席位锁移交给预备事务
             └─ COMMIT/ROLLBACK PREPARED（网页或库端直接执行）── PG 自动还席

任何 PREPARE 之前的失败/退出路径都必须 rollback+close 席位连接，保证还席。
"""
from __future__ import annotations

import threading
import traceback
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2

from . import config, recovery, sample, slots, sqlite_store
from .pgdb import pg_conn

# 演示固定使用的两个参数（db/01_schema.sql 幂等播种）
BULK_PARAM_NO = "CAL-BULK-1001"
BULK_DEVICE_NO = "DEV-BULK-01"
EMERGENCY_PARAM_NO = "CAL-EMRG-0001"
EMERGENCY_DEVICE_NO = "DEV-EMG-01"


class PublishError(Exception):
    """可向值班员展示的下单失败（非满额类）。"""


# ------------------------------------------------------------------ 基础工具

def _connect_seat(gxid: str, device_no: str) -> psycopg2.extensions.connection:
    safe_device = (device_no or "UNKNOWN").strip()[:24]
    app_name = f"{slots.APP_PREFIX}:{gxid}:{safe_device}"
    conn = psycopg2.connect(
        config.DATABASE_URL,
        connect_timeout=5,
        application_name=app_name,
    )
    conn.autocommit = False  # 第一条语句自动 BEGIN，席位锁在该事务内持有
    return conn


def _read_value(cur, param_no: str) -> tuple[str, str | None]:
    cur.execute(
        "SELECT param_value, updated_gxid FROM calibration_param WHERE param_no=%s",
        (param_no,),
    )
    row = cur.fetchone()
    if row is None:
        raise PublishError(f"参数 {param_no} 不存在，无法发布。")
    return row[0], row[1]


@contextmanager
def held_seat(device_no: str) -> Iterator[tuple[Any, int]]:
    """测试/底层辅助：占用一个席位并把 ``(conn, slot_no)`` 交给调用方。

    退出 with 块时 rollback+close，席位立即归还。
    """
    conn = _connect_seat("GX-SEAT-HELPER", device_no or "helper")
    try:
        slot_no = slots.try_acquire(conn, device_no)
        yield conn, slot_no
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 核心下单

def run_publish(
    *,
    device_no: str,
    param_no: str | None = None,
    intended_value: str | None = None,
    reason: str = "",
    gxid: str | None = None,
    lock_timeout_ms: int | None = None,
    fail_after_order: bool = False,
    crash_before_prepare: bool = False,
) -> dict[str, Any]:
    """受理并执行一笔发布，直到 ``PREPARE TRANSACTION``（或在预备前失败）。

    满额时抛 :class:`slots.SlotsFullError`；其它业务失败抛 :class:`PublishError`。
    成功返回包含 ``gxid / slot_no / seat_kind`` 的结果字典。
    """
    device_no = (device_no or "").strip()
    if not device_no:
        raise PublishError("必须填写设备号。")

    # 配置超出 PostgreSQL 上限属于配置错误，先拦截。
    check = slots.validate_total_against_pg()
    if not check["ok"]:
        raise slots.SlotConfigError("；".join(check["problems"]))

    gxid = gxid or sample.new_gxid()
    recovery.validate_gxid(gxid)

    emergency = slots.is_emergency_device(device_no)
    conn = _connect_seat(gxid, device_no)
    slot_no: int | None = None
    try:
        # 1) 先占席：满额立刻拒绝，不做任何后续操作。
        slot_no = slots.try_acquire(conn, device_no)
        seat_kind = "reserved" if slot_no == slots.reserved_slot_no() else "general"
        sqlite_store.add_event(
            gxid, "SLOT_ACQUIRED",
            f"建单受理即占第 {slot_no} 席（{ '抢修预留席' if seat_kind == 'reserved' else '普通席'}，设备 {device_no}）",
            "publish-desk",
        )

        with conn.cursor() as cur:
            # 2) 解析参数与前像值（在占席事务内读，快照一致）。
            if param_no is None:
                # 独立连接创建参数并提交；随后在本事务内可见（新语句取新快照）。
                param_no = sample.create_demo_param()
            baseline, _ = _read_value(cur, param_no)
            target = intended_value.strip() if intended_value and intended_value.strip() else sample._bump(baseline)
            if target == baseline:
                target = baseline + ".1"

        # 3) 发布单独立事务提交：即使后面 ROLLBACK PREPARED，前像证据仍在。
        with pg_conn() as order_conn:
            with order_conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO publish_order(gxid, param_no, intended_value, baseline_value, reason)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (gxid) DO NOTHING""",
                    (gxid, param_no, target, baseline, reason or "发布台受理"),
                )

        if fail_after_order:
            # 模拟“建单失败/中途退出（尚未 PREPARE）”：rollback 席位事务后还席。
            raise PublishError("模拟建单中途失败（发布单已留，但尚未进入预备态）。")

        # 4) 参数行更新（可能在这里阻塞等行锁 —— 席位 advisory lock 已持有，不会丢）。
        with conn.cursor() as cur:
            if lock_timeout_ms is None:
                lock_timeout_ms = config.PUBLISH_LOCK_TIMEOUT_MS
            if lock_timeout_ms and lock_timeout_ms > 0:
                cur.execute("SET LOCAL lock_timeout = %s", (f"{int(lock_timeout_ms)}ms",))
            cur.execute(
                """UPDATE calibration_param
                      SET param_value=%s, version=version+1, updated_gxid=%s, updated_at=now()
                    WHERE param_no=%s""",
                (target, gxid, param_no),
            )
            if cur.rowcount != 1:
                raise PublishError(f"参数 {param_no} 不存在，无法发布。")
            cur.execute(
                "INSERT INTO param_revision(gxid, param_no, param_value) VALUES (%s,%s,%s)",
                (gxid, param_no, target),
            )
            cur.execute("INSERT INTO txn_commit_marker(gxid) VALUES (%s)", (gxid,))
            if crash_before_prepare and config.ENABLE_CRASH_DEMO:
                # 仅限演示：读写都已完成、恰在 PREPARE 前 os._exit(43)。
                # PG 发现连接退出后回滚该事务，advisory 席位锁与行锁一并释放。
                import os

                os._exit(43)
            # 5) PREPARE：席位锁移交给预备事务；gxid 已过白名单，可安全拼接。
            cur.execute(f"PREPARE TRANSACTION '{gxid}'")

        sqlite_store.add_event(
            gxid, "PREPARED",
            f"已进入预备态，第 {slot_no} 席由预备事务继续持有（网页重启不释放）",
            "publish-desk",
        )
        return {
            "gxid": gxid,
            "slot_no": slot_no,
            "seat_kind": seat_kind,
            "emergency": emergency,
            "device_no": device_no,
            "param_no": param_no,
            "baseline": baseline,
            "intended": target,
            "state": "PREPARED",
        }
    except Exception:
        # 预备前任何失败：回滚未决事务 -> 释放 advisory xact lock（席位）。
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 行锁道具

BLOCKERS: dict[str, dict[str, Any]] = {}
_blocker_lock = threading.Lock()


def hold_row_lock(param_no: str, key: str = "demo-bulk", ttl_seconds: int | None = None) -> dict[str, Any]:
    """另开一个会话长期持有指定参数行的行锁，用来制造“普通建单等锁”。"""
    ttl_seconds = config.DEMO_LOCK_HOLD_SECONDS if ttl_seconds is None else ttl_seconds
    with _blocker_lock:
        if key in BLOCKERS:
            release_row_lock(key)
        conn = psycopg2.connect(config.DATABASE_URL, connect_timeout=5, application_name=f"calpub-blocker:{key}")
        conn.autocommit = False
        cur = conn.cursor()
        # 用 FOR NO KEY UPDATE（与真实 UPDATE 非键列后持有的行锁同级冲突）：
        # 它会挡住普通发布的 UPDATE，但不与 publish_order 外键检查的 FOR KEY SHARE 冲突，
        # 因此普通发布会准确阻塞在参数 UPDATE（等行锁）这一步，而不是外键校验上。
        cur.execute(
            "SELECT param_no FROM calibration_param WHERE param_no=%s FOR NO KEY UPDATE",
            (param_no,),
        )
        if cur.fetchone() is None:
            conn.rollback()
            conn.close()
            raise PublishError(f"无法对不存在的参数 {param_no} 加锁。")
        item: dict[str, Any] = {"key": key, "param_no": param_no, "conn": conn, "timer": None}

        def _auto_release() -> None:
            try:
                release_row_lock(key)
            except Exception:
                pass

        if ttl_seconds and ttl_seconds > 0:
            item["timer"] = threading.Timer(ttl_seconds, _auto_release)
            item["timer"].daemon = True
            item["timer"].start()
        BLOCKERS[key] = item
        return {"key": key, "param_no": param_no, "held": True, "ttl_seconds": ttl_seconds}


def release_row_lock(key: str = "demo-bulk") -> bool:
    with _blocker_lock:
        item = BLOCKERS.pop(key, None)
    if item is None:
        return False
    timer = item.get("timer")
    if timer is not None:
        timer.cancel()
    conn = item["conn"]
    try:
        conn.commit()  # 提交即释放行锁，等待中的普通发布会被唤醒
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass
    return True


def blocker_status() -> list[dict[str, Any]]:
    return [{"key": k, "param_no": v["param_no"], "held": True} for k, v in BLOCKERS.items()]


# ------------------------------------------------------------------ 后台发布任务

JOBS: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def start_publish_job(**kwargs: Any) -> str:
    """在守护线程里跑 :func:`run_publish`（用于“普通建单持续等锁”演示）。"""
    gxid = kwargs.get("gxid") or sample.new_gxid()
    kwargs["gxid"] = gxid
    job: dict[str, Any] = {
        "gxid": gxid,
        "kwargs": {k: v for k, v in kwargs.items() if k != "crash_before_prepare"},
        "state": "running",
        "result": None,
        "error": None,
        "code": None,
        "thread": None,
    }

    def _target() -> None:
        try:
            result = run_publish(**kwargs)
            job["state"] = "prepared"
            job["result"] = result
        except slots.SlotsFullError as exc:
            job["state"] = "rejected"
            job["code"] = exc.code
            job["error"] = exc.message
        except Exception as exc:  # noqa: BLE001 - 演示任务需要把异常带回页面
            job["state"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["traceback"] = traceback.format_exc(limit=3)

    t = threading.Thread(target=_target, name=f"publish-{gxid}", daemon=True)
    job["thread"] = t
    with _jobs_lock:
        JOBS[gxid] = job
    t.start()
    return gxid


def get_job(gxid: str) -> dict[str, Any] | None:
    return JOBS.get(gxid)


def list_jobs() -> list[dict[str, Any]]:
    return [
        {k: v for k, v in j.items() if k not in ("thread", "kwargs")}
        | {"alive": bool(j.get("thread") and j["thread"].is_alive())}
        for j in JOBS.values()
    ]


def wait_for_job(gxid: str, expected_state: str = "prepared", timeout: float = 10.0) -> dict[str, Any]:
    deadline = threading.Event()
    step = 0.1
    waited = 0.0
    while waited < timeout:
        job = JOBS.get(gxid)
        if job and job["state"] == expected_state:
            return job
        if job and job["state"] in ("error", "rejected") and expected_state not in ("error", "rejected"):
            raise PublishError(job.get("error") or f"任务进入 {job['state']}")
        deadline.wait(step)
        waited += step
    raise TimeoutError(f"等待发布任务 {gxid} 进入 {expected_state} 超时（当前 {JOBS.get(gxid, {}).get('state')}）")


def wait_slot_phase(gxid: str, phase: str, timeout: float = 6.0) -> dict[str, Any]:
    """轮询名额看板，直到指定 gxid 的席位进入指定 phase（如 waiting_lock）。"""
    waited = 0.0
    last: dict[str, Any] | None = None
    while waited < timeout:
        snap = slots.status()
        for s in snap["slots"]:
            if s.get("gxid") == gxid:
                last = s
                if s["phase"] == phase:
                    return s
        threading.Event().wait(0.1)
        waited += 0.1
    raise TimeoutError(f"席位 {gxid} 未在 {timeout}s 内进入 {phase}；最后状态={last}")


def reset_runtime(join_timeout: float = 8.0) -> None:
    """测试用：释放道具锁并等待所有后台发布线程收尾到 PREPARE，再清空登记表。"""
    for key in list(BLOCKERS.keys()):
        release_row_lock(key)
    threads = [j.get("thread") for j in JOBS.values()]
    for t in threads:
        if t is not None:
            t.join(join_timeout)
    JOBS.clear()


# ------------------------------------------------------------------ 库端直接收尾

def dba_direct_prepared(gxid: str, choice: str) -> dict[str, Any]:
    """模拟 DBA/库端直接 COMMIT/ROLLBACK PREPARED —— 不写网页登记簿。

    PostgreSQL 会在工具语句成功的同时自动释放席位 advisory lock。
    """
    gxid = recovery.validate_gxid(gxid)
    if choice not in ("COMMIT", "ROLLBACK"):
        raise PublishError("choice 必须是 COMMIT 或 ROLLBACK。")
    sql = f"{choice} PREPARED {recovery._quote_gid(gxid)}"
    with pg_conn(autocommit=True) as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(sql)
                done = True
            except psycopg2.errors.UndefinedObject:
                conn.rollback()
                done = False
    snap = slots.status()
    slot = next((s for s in snap["slots"] if s.get("gxid") == gxid), None)
    return {
        "gxid": gxid,
        "choice": choice,
        "executed": done,  # False = 预备事务此前已不在库中
        "slot_still_occupied": slot is not None and slot["occupied"],
        "slots": snap,
    }


# ------------------------------------------------------------------ 一键抢修演示

LAST_DEMO: dict[str, Any] | None = None


def run_emergency_demo() -> dict[str, Any]:
    """构造“2 席（1 普通 + 1 预留）下普通集中发布排队等锁、抢修放行”的完整剧本。

    返回可直接渲染/JSON 输出的结构化结果。
    """
    # 0) 干净起步：释放上一轮道具锁/后台任务；若上一轮的普通发布已 PREPARE 且仍占席，
    #    作为“演示道具”直接库端回滚，保证一键剧本可以重复运行。
    reset_runtime()
    if LAST_DEMO:
        old_gxid = (LAST_DEMO.get("ordinary") or {}).get("gxid")
        if old_gxid:
            try:
                dba_direct_prepared(old_gxid, "ROLLBACK")
            except Exception:
                pass

    # 1) 用一个外部会话占住集中发布参数的行锁，模拟前一笔发布/批量任务未提交。
    hold_row_lock(BULK_PARAM_NO, key="demo-bulk")

    # 2) 第一笔普通发布在后台受理：拿到第 1 席（普通席），随后阻塞在参数行锁上。
    ordinary_gxid = start_publish_job(
        device_no=BULK_DEVICE_NO,
        param_no=BULK_PARAM_NO,
        reason="普通设备集中发布批次（等前一行锁）",
        lock_timeout_ms=120_000,  # 演示页留 2 分钟人工操作；超时也会安全还席而非永久挂住
    )
    ordinary_slot = wait_slot_phase(ordinary_gxid, "waiting_lock", timeout=8.0)
    # 给这笔普通发布预写 COMMIT 决定，稍后释放行锁、PREPARE 后可在恢复页一键收尾。
    sqlite_store.write_decision(ordinary_gxid, "COMMIT", "集中发布批次校验通过（演示预写）", "coordinator-demo")

    # 3) 并发的第二笔普通建单：普通席已满 -> 立刻拒绝（预留席不对它开放）。
    rejection: dict[str, Any]
    try:
        run_publish(
            device_no="DEV-BULK-02",
            param_no=BULK_PARAM_NO,
            reason="并发普通建单（应被满额拒绝）",
            lock_timeout_ms=1000,
        )
        rejection = {"code": "NOT_REJECTED", "message": "预期被拒绝，但竟然受理成功。"}
    except slots.SlotsFullError as exc:
        rejection = {"code": exc.code, "message": exc.message}

    # 4) 抢修设备建单：绕过占满的普通席，直接使用第 2 席（抢修预留席），
    #    发布到它自己的参数行（无行锁冲突），并快速完成 2PC 提交、更新校准值。
    before = _param_value(EMERGENCY_PARAM_NO)
    emg = run_publish(
        device_no=EMERGENCY_DEVICE_NO,
        param_no=EMERGENCY_PARAM_NO,
        reason="抢修设备校准参数紧急更新",
    )
    sqlite_store.write_decision(emg["gxid"], "COMMIT", "抢修放行：优先校验通过", "coordinator-demo")
    finalized = recovery.finalize_from_decision(emg["gxid"], operator="emergency-desk")
    after = _param_value(EMERGENCY_PARAM_NO)
    snap_after = slots.status()

    return {
        "bulk_param": BULK_PARAM_NO,
        "emergency_param": EMERGENCY_PARAM_NO,
        "ordinary": {
            "gxid": ordinary_gxid,
            "slot_no": ordinary_slot["slot_no"],
            "phase": ordinary_slot["phase"],
            "phase_label": ordinary_slot["phase_label"],
        },
        "rejected_ordinary": rejection,
        "emergency": {
            **emg,
            "value_before": before,
            "value_after": after,
            "finalized_idempotent": finalized.get("idempotent", False),
        },
        "slots_after_emergency_commit": snap_after,
        "note": "普通发布仍在第 1 席等锁；抢修发布已用第 2 席完成提交并归还预留席。",
    }


def _param_value(param_no: str) -> str | None:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT param_value FROM calibration_param WHERE param_no=%s", (param_no,))
            row = cur.fetchone()
    return row[0] if row else None
