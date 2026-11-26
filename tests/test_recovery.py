"""关键场景试跑：挂起/待确认/按决定收尾/重启证据区分/冲突/待核实/幂等/并发。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import recovery, sample, sqlite_store
from app.pgdb import pg_conn


# ------------------------------------------------------------ 辅助断言

def _param_value(param_no: str):
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT param_value, version, updated_gxid FROM calibration_param WHERE param_no=%s",
                (param_no,),
            )
            return cur.fetchone()


def _revision_count(gxid: str) -> int:
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM param_revision WHERE gxid=%s", (gxid,))
            return cur.fetchone()[0]


def _make(kind: str):
    gxid = sample.create_scenario(kind)
    snap = recovery.inspect(gxid)
    param_no = snap["order"]["param_no"]
    return gxid, param_no, snap


# ------------------------------------------------------------ 页面可打开

def test_pages_render(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "校准参数" in r.text
    assert "CAL-T-1001" in r.text

    r = client.get("/search?q=CAL-T")
    assert r.status_code == 200
    assert "CAL-T-1001" in r.text

    r = client.get("/search?q=__nope__")
    assert r.status_code == 200

    r = client.get("/params/CAL-T-1001")
    assert r.status_code == 200
    assert "温度探头零点偏移" in r.text

    r = client.get("/params/NO-SUCH")
    assert r.status_code == 404

    r = client.get("/txn/GX-DOES-NOT-EXIST-999")
    assert r.status_code == 404

    assert client.get("/healthz").json() == {"status": "ok"}
    api = client.get("/api/inspect/GX-DOES-NOT-EXIST-999").json()
    assert api["state"] == "NOT_FOUND"


# ------------------------------------------------------------ 场景③：无决定 -> 保留与待确认

def test_open_no_decision_keeps_txn_and_locks(client):
    gxid, param_no, snap = _make("open_no_decision")
    assert snap["state"] == "OPEN_AWAITING"
    assert snap["actual"] == "OPEN"
    assert snap["decision"] is None
    assert snap["prepared"] is not None
    # 锁证据：至少有 relation RowExclusiveLock 与 transactionid ExclusiveLock
    modes = {(l["locktype"], l["mode"]) for l in snap["locks"]}
    assert ("relation", "RowExclusiveLock") in modes
    assert ("transactionid", "ExclusiveLock") in modes

    baseline = snap["order"]["baseline_value"]
    # 其他会话看到的仍是基线值（未提交）
    assert _param_value(param_no)[0] == baseline

    # 直接调 finalize 必须被拒绝，事务与锁原样保留
    with pytest.raises(recovery.RecoveryError):
        recovery.finalize_from_decision(gxid)
    again = recovery.inspect(gxid)
    assert again["actual"] == "OPEN"
    assert again["state"] == "OPEN_AWAITING"
    assert again["record"] is None

    # HTTP：POST finalize 被重定向回错误提示，事务仍在
    r = client.post(f"/txn/{gxid}/finalize", data={"operator": "值班员甲"}, follow_redirects=False)
    assert r.status_code == 303
    assert recovery.inspect(gxid)["actual"] == "OPEN"

    # 人工确认提交并收尾
    out = recovery.manual_decision_and_finalize(gxid, "COMMIT", "值班员甲", "电话确认 OPS-1")
    assert out["idempotent"] is False
    val, version, upd = _param_value(param_no)
    assert val == snap["order"]["intended_value"]
    assert upd == gxid
    assert recovery.inspect(gxid)["state"] == "DONE_COMMITTED"
    # 决定记录为人工补录
    d = sqlite_store.get_decision(gxid)
    assert d["decided_by"] == "manual:值班员甲"


def test_manual_abort_path(client):
    gxid, param_no, snap = _make("open_no_decision")
    baseline = snap["order"]["baseline_value"]
    recovery.manual_decision_and_finalize(gxid, "ABORT", "值班员乙", "设备超差")
    val, version, upd = _param_value(param_no)
    assert val == baseline
    assert upd is None or upd != gxid
    assert recovery.inspect(gxid)["state"] == "DONE_ROLLED_BACK"


# ------------------------------------------------------------ 场景①②：按 SQLite 决定收尾 + 幂等

def test_open_commit_decision_finalize_is_idempotent(client):
    gxid, param_no, snap = _make("open_commit")
    assert snap["state"] == "OPEN_COMMIT"

    r = client.post(f"/txn/{gxid}/finalize", data={"operator": "wang"}, follow_redirects=False)
    assert r.status_code == 303

    s1 = recovery.inspect(gxid)
    assert s1["state"] == "DONE_COMMITTED"
    assert s1["record"]["handling"] == "FINALIZED"
    intended = s1["order"]["intended_value"]
    assert _param_value(param_no)[0] == intended
    assert _revision_count(gxid) == 1

    before = _param_value(param_no)
    # 重复办理（服务层 + HTTP）不得再次修改参数
    out2 = recovery.finalize_from_decision(gxid, operator="wang")
    assert out2["idempotent"] is True
    r2 = client.post(f"/txn/{gxid}/finalize", data={"operator": "wang"}, follow_redirects=True)
    assert "重复办理" in r2.text
    after = _param_value(param_no)
    assert before == after
    assert _revision_count(gxid) == 1


def test_open_abort_decision_rolls_back(client):
    gxid, param_no, snap = _make("open_abort")
    assert snap["state"] == "OPEN_ABORT"
    recovery.finalize_from_decision(gxid)
    assert _param_value(param_no)[0] == snap["order"]["baseline_value"]
    assert _revision_count(gxid) == 0
    assert recovery.inspect(gxid)["state"] == "DONE_ROLLED_BACK"

    # 重复回滚是幂等返回，不会报错也不会再动
    out = recovery.finalize_from_decision(gxid)
    assert out["idempotent"] is True


def test_concurrent_finalize_only_once(client):
    gxid, param_no, snap = _make("open_commit")
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        return recovery.finalize_from_decision(gxid, operator="pool")

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda _: go(), range(6)))

    assert sum(1 for r in results if not r["idempotent"]) == 1
    assert sum(1 for r in results if r["idempotent"]) == 5
    assert _revision_count(gxid) == 1
    assert _param_value(param_no)[0] == snap["order"]["intended_value"]


# ------------------------------------------------------------ 场景④⑤：库端已收尾、网页未登记 -> 证据区分

def test_db_committed_unregistered_reconciles(client):
    gxid, param_no, snap = _make("committed_unregistered")
    assert snap["actual"] == "COMMITTED"
    assert snap["state"] == "RECONCILE_COMMIT"
    assert snap["record"] is None
    # 强证据存在
    assert snap["marker"] is not None
    assert snap["revision"] is not None

    value_before = _param_value(param_no)
    out = recovery.register_from_evidence(gxid)
    assert out["idempotent"] is False
    # 补登记绝不改参数
    assert _param_value(param_no) == value_before

    s = recovery.inspect(gxid)
    assert s["state"] == "DONE_COMMITTED"
    # 重复补登记幂等
    assert recovery.register_from_evidence(gxid)["idempotent"] is True


def test_db_rolledback_unregistered_reconciles(client):
    gxid, param_no, snap = _make("rolledback_unregistered")
    assert snap["actual"] == "ROLLED_BACK"
    assert snap["state"] == "RECONCILE_ABORT"
    assert snap["marker"] is None
    assert snap["revision"] is None
    assert _param_value(param_no)[0] == snap["order"]["baseline_value"]

    r = client.post(f"/txn/{gxid}/register", follow_redirects=False)
    assert r.status_code == 303
    assert recovery.inspect(gxid)["state"] == "DONE_ROLLED_BACK"


# ------------------------------------------------------------ 场景⑥⑦：决定与事实冲突

def test_conflict_decision_commit_but_db_rollback(client):
    gxid, param_no, snap = _make("conflict_commit_vs_rollback")
    assert snap["state"] == "CONFLICT_COMMIT_BUT_ABORTED"
    assert _param_value(param_no)[0] == snap["order"]["baseline_value"]

    # 普通补登记必须被拒绝
    with pytest.raises(recovery.RecoveryError):
        recovery.register_from_evidence(gxid)

    r = client.post(
        f"/txn/{gxid}/conflict",
        data={"operator": "值班员赵", "note": "已开工单 OPS-9"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = recovery.inspect(gxid)
    assert s["state"] == "DONE_CONFLICT"
    assert s["record"]["outcome"] == "CONFLICT"
    # 参数没有被“按决定”再次提交
    assert _param_value(param_no)[0] == snap["order"]["baseline_value"]
    # 重复冲突登记幂等
    out = recovery.ack_conflict(gxid, "值班员赵", "x")
    assert out["idempotent"] is True


def test_conflict_decision_abort_but_db_commit(client):
    gxid, param_no, snap = _make("conflict_abort_vs_commit")
    assert snap["state"] == "CONFLICT_ABORT_BUT_COMMITTED"
    assert _param_value(param_no)[0] == snap["order"]["intended_value"]
    recovery.ack_conflict(gxid, "z", "事实提交")
    assert recovery.inspect(gxid)["state"] == "DONE_CONFLICT"


# ------------------------------------------------------------ 场景⑧：证据不足 -> 待核实

def test_unverified_marked_and_manual_verify(client):
    gxid, param_no, snap = _make("unverified_overwrite")
    assert snap["actual"] == "UNVERIFIED"
    assert snap["state"] == "UNVERIFIED"

    with pytest.raises(recovery.RecoveryError):
        recovery.register_from_evidence(gxid)

    r = client.post(
        f"/txn/{gxid}/verify",
        data={"claimed": "ROLLED_BACK", "operator": "专家钱", "note": "设备回执显示旧值"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    s = recovery.inspect(gxid)
    assert s["state"] == "DONE_MANUAL"
    assert s["record"]["handling"] == "MANUAL_VERIFY"
    assert s["record"]["outcome"] == "ROLLED_BACK"
    assert recovery.manual_verify(gxid, "COMMITTED", "x", "")["idempotent"] is True


# ------------------------------------------------------------ 锁演示接口不改数据

def test_lock_demo_reports_free_or_locked(client):
    # 无挂起事务的普通种子参数：应当能拿到锁
    out = recovery.probe_row_lock("CAL-T-1001", timeout_ms=1500)
    assert out["acquired"] is True
    val = _param_value("CAL-T-1001")
    assert val[0] == "0.1250"  # 原样

    # 有挂起事务的参数：应当被阻塞（或被客户端硬超时判定 blocked）
    gxid, param_no, snap = _make("open_no_decision")
    out = recovery.probe_row_lock(param_no, timeout_ms=1500)
    assert out["acquired"] is False
    assert out.get("sqlstate") == "55P03" or out.get("client_timeout")
    # 预备事务依然在，值依然是基线
    assert recovery.inspect(gxid)["actual"] == "OPEN"
    assert _param_value(param_no)[0] == snap["order"]["baseline_value"]

    # HTTP 路由
    r = client.post(f"/params/{param_no}/lock-demo", follow_redirects=False)
    assert r.status_code == 303
    assert "lock=blocked" in r.headers["location"]
