"""发布台名额关键场景：

- 2 席布局：1 普通 + 1 抢修预留；剩余名额实时统计
- 并发普通建单满额即拒绝（GENERAL_SLOTS_FULL），预留席不对普通设备开放
- 普通发布等参数行锁时仍占席；抢修发布走预留席放行
- 建单失败（未 PREPARE）立即还席
- PREPARE 后席位跨“网页重启”保留；库端直接 COMMIT/ROLLBACK PREPARED 后自动还席，无需网页登记
- HTTP/JSON 接口 409 与一键抢修演示
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import config, publish, slots, sqlite_store


@pytest.fixture(autouse=True)
def _clean_publish_runtime():
    yield
    # 先释放道具行锁并等后台发布走到 PREPARE，避免阻塞 conftest 的清表
    publish.reset_runtime(join_timeout=8)


def _rollback_all_slot_prepared():
    """兜底：直接库端回滚所有 GX 预备事务（conftest 也会做）。"""
    from app.pgdb import pg_conn

    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT gid FROM pg_prepared_xacts WHERE gid LIKE 'GX-%'")
            for (gid,) in cur.fetchall():
                cur.execute(f"ROLLBACK PREPARED '{gid}'")


# ------------------------------------------------------------ 布局/上限

def test_slot_layout_default_two_with_reserved_last(client):
    assert config.PUBLISH_MAX_SLOTS == 2
    snap = slots.status()
    assert snap["total"] == 2
    assert snap["general_total"] == 1
    assert snap["reserved_slot_no"] == 2
    assert [s["slot_no"] for s in snap["slots"]] == [1, 2]
    assert all(not s["occupied"] for s in snap["slots"])
    assert snap["remaining"] == 2
    assert slots.is_emergency_device("DEV-EMG-01")
    assert not slots.is_emergency_device("DEV-BULK-01")

    check = slots.validate_total_against_pg()
    assert check["ok"] is True, check["problems"]
    assert check["max_prepared_transactions"] >= 2


def test_layout_scales_with_config(monkeypatch, client):
    monkeypatch.setattr(config, "PUBLISH_MAX_SLOTS", 5)
    assert slots.general_slot_numbers() == [1, 2, 3, 4]
    assert slots.reserved_slot_no() == 5
    snap = slots.status()
    assert snap["total"] == 5 and len(snap["slots"]) == 5
    assert snap["general_total"] == 4
    # 抢修设备候选顺序：先预留席 5，再普通席
    assert slots.candidate_slots("DEV-EMG-01") == [5, 1, 2, 3, 4]
    assert slots.candidate_slots("DEV-X") == [1, 2, 3, 4]


# ------------------------------------------------------------ 满额拒绝 + 预留

def test_ordinary_full_rejected_then_emergency_uses_reserved(client):
    p1 = publish.run_publish(device_no="DEV-BULK-01", param_no=publish.BULK_PARAM_NO)
    assert p1["slot_no"] == 1 and p1["seat_kind"] == "general"

    # 第二个普通建单：唯一普通席已占 -> 拒绝，原因明确提到预留席
    with pytest.raises(slots.SlotsFullError) as ei:
        publish.run_publish(device_no="DEV-BULK-02", param_no=publish.BULK_PARAM_NO)
    assert ei.value.code == "GENERAL_SLOTS_FULL"
    assert "预留" in ei.value.message and "DEV-EMG-01" in ei.value.message

    snap = slots.status()
    assert snap["general_used"] == 1 and snap["general_remaining"] == 0
    assert snap["reserved_used"] == 0 and snap["remaining"] == 1

    # 抢修设备：拿到第 2 席（预留席）
    emg = publish.run_publish(device_no="DEV-EMG-01", param_no=publish.EMERGENCY_PARAM_NO)
    assert emg["slot_no"] == 2 and emg["seat_kind"] == "reserved"

    snap = slots.status()
    assert snap["used"] == 2 and snap["remaining"] == 0

    # 两席全占后，抢修也被拒绝（ALL_SLOTS_FULL）
    with pytest.raises(slots.SlotsFullError) as ei2:
        publish.run_publish(device_no="DEV-EMG-01", param_no=publish.EMERGENCY_PARAM_NO)
    assert ei2.value.code == "ALL_SLOTS_FULL"


def test_concurrent_ordinary_orders_exactly_one_gets_seat(client):
    barrier = threading.Barrier(6)
    outcomes: list[str] = []
    lock = threading.Lock()

    def go(i):
        barrier.wait()
        try:
            r = publish.run_publish(
                device_no=f"DEV-BULK-{i:02d}", param_no=publish.BULK_PARAM_NO
            )
            with lock:
                outcomes.append(f"ok:{r['slot_no']}")
        except slots.SlotsFullError as exc:
            with lock:
                outcomes.append(f"full:{exc.code}")

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(go, range(6)))

    oks = [o for o in outcomes if o.startswith("ok:")]
    fulls = [o for o in outcomes if o.startswith("full:")]
    assert len(oks) == 1, outcomes
    assert oks[0] == "ok:1"
    assert len(fulls) == 5
    assert all(o == "full:GENERAL_SLOTS_FULL" for o in fulls)
    assert slots.status()["general_used"] == 1


# ------------------------------------------------------------ 等锁仍占席 + 抢修放行

def test_ordinary_waiting_on_row_lock_keeps_seat_emergency_passes(client):
    publish.hold_row_lock(publish.BULK_PARAM_NO, key="test-lock")

    gxid = publish.start_publish_job(
        device_no="DEV-BULK-01",
        param_no=publish.BULK_PARAM_NO,
        lock_timeout_ms=8000,
    )
    seat = publish.wait_slot_phase(gxid, "waiting_lock", timeout=8)
    assert seat["slot_no"] == 1

    snap = slots.status()
    assert snap["used"] == 1 and snap["slots"][0]["phase"] == "waiting_lock"

    # 普通并发建单：普通席（被等锁者占着）已满，拒绝
    with pytest.raises(slots.SlotsFullError) as ei:
        publish.run_publish(device_no="DEV-BULK-09", param_no=publish.BULK_PARAM_NO)
    assert ei.value.code == "GENERAL_SLOTS_FULL"

    # 抢修发布不受影响：用预留席发布自己的参数，快速完成 PREPARE
    t0 = time.monotonic()
    emg = publish.run_publish(device_no="DEV-EMG-01", param_no=publish.EMERGENCY_PARAM_NO)
    elapsed = time.monotonic() - t0
    assert emg["slot_no"] == 2
    assert elapsed < 5.0  # 没有被集中发布的行锁/名额挡住

    # 释放行锁 -> 普通发布被唤醒并进入 PREPARE，席位仍在（转为 prepared 持有）
    publish.release_row_lock("test-lock")
    job = publish.wait_for_job(gxid, "prepared", timeout=10)
    assert job["result"]["slot_no"] == 1
    phase = next(s for s in slots.status()["slots"] if s["slot_no"] == 1)
    assert phase["phase"] == "prepared" and phase["gxid"] == gxid


# ------------------------------------------------------------ 失败还席

def test_failure_before_prepare_releases_slot(client):
    before = slots.status()["remaining"]
    with pytest.raises(publish.PublishError):
        publish.run_publish(device_no="DEV-BULK-01", fail_after_order=True)
    after = slots.status()
    assert after["remaining"] == before
    assert after["used"] == 0

    # held_seat 底层上下文退出（rollback+close）也必须立即还席
    with publish.held_seat("DEV-BULK-01") as (_conn, no):
        assert no == 1
        assert slots.status()["general_used"] == 1
    assert slots.status()["general_used"] == 0


# ------------------------------------------------------------ 重启保留 + 库端直接收尾还席

def test_prepared_slot_survives_restart_and_direct_db_rollback_frees_it(client):
    p = publish.run_publish(device_no="DEV-BULK-01", param_no=publish.BULK_PARAM_NO)
    gxid = p["gxid"]

    # 模拟网页重启：清空所有进程内登记表/任务；名额状态完全来自 PG
    publish.JOBS.clear()
    publish.BLOCKERS.clear()

    snap = slots.status()
    s1 = next(s for s in snap["slots"] if s["slot_no"] == 1)
    assert s1["occupied"] and s1["phase"] == "prepared" and s1["gxid"] == gxid

    # 库端/DBA 直接 ROLLBACK PREPARED，不经过网页、不写登记簿
    out = publish.dba_direct_prepared(gxid, "ROLLBACK")
    assert out["executed"] is True and out["slot_still_occupied"] is False
    assert sqlite_store.get_record(gxid) is None  # 网页确实没有补登记

    snap = slots.status()
    assert snap["used"] == 0 and snap["remaining"] == 2
    assert not any(s.get("gxid") == gxid for s in snap["slots"])


def test_direct_commit_prepared_also_frees_slot(client):
    emg = publish.run_publish(device_no="DEV-EMG-01", param_no=publish.EMERGENCY_PARAM_NO)
    assert emg["slot_no"] == 2
    out = publish.dba_direct_prepared(emg["gxid"], "COMMIT")
    assert out["executed"] is True
    assert slots.status()["reserved_used"] == 0
    # 参数确实已更新（库端直接提交）
    from app.pgdb import pg_conn

    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT updated_gxid FROM calibration_param WHERE param_no=%s",
                        (publish.EMERGENCY_PARAM_NO,))
            assert cur.fetchone()[0] == emg["gxid"]


# ------------------------------------------------------------ HTTP / JSON

def test_publish_page_and_slots_api(client):
    r = client.get("/publish")
    assert r.status_code == 200
    assert "抢修预留席" in r.text and "剩余名额" in r.text

    data = client.get("/api/slots").json()
    assert data["total"] == 2 and data["remaining"] == 2
    assert data["config_check"]["ok"] is True

    assert client.get("/api/publish/slots").json()["total"] == 2
    assert "/publish" in client.get("/").text


def test_fail_demo_route_returns_slot(client):
    r = client.post(
        "/publish/fail-demo",
        data={"device_no": "DEV-BULK-01"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "已触发“建单失败" in r.text
    assert "名额已归还" in r.text
    assert slots.status()["remaining"] == 2


def test_http_json_order_conflict_and_dba_release(client):
    # 普通受理 -> 200，占 1 席
    r = client.post("/api/publish/orders", json={
        "device_no": "DEV-BULK-01", "param_no": publish.BULK_PARAM_NO,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    gxid = body["publish"]["gxid"]
    assert body["publish"]["slot_no"] == 1
    assert body["slots"]["used"] == 1

    # 第二个普通 -> 409 + 拒绝原因
    r2 = client.post("/api/publish/orders", json={
        "device_no": "DEV-BULK-02", "param_no": publish.BULK_PARAM_NO,
    })
    assert r2.status_code == 409
    j2 = r2.json()
    assert j2["code"] == "GENERAL_SLOTS_FULL" and "预留" in j2["error"]
    assert j2["slots"]["general_used"] == 1

    # 抢修 -> 200 第 2 席
    r3 = client.post("/api/publish/orders", json={
        "device_no": "DEV-EMG-01", "param_no": publish.EMERGENCY_PARAM_NO,
    })
    assert r3.status_code == 200
    assert r3.json()["publish"]["slot_no"] == 2

    # 满额后抢修也 409
    r4 = client.post("/api/publish", json={"device_no": "DEV-EMG-01"})
    assert r4.status_code == 409 and r4.json()["code"] == "ALL_SLOTS_FULL"

    # 库端直接撤销两笔 -> 名额全释放
    for g in (gxid, r3.json()["publish"]["gxid"]):
        rr = client.post(f"/publish/dba/{g}/rollback", follow_redirects=False)
        assert rr.status_code == 303
    assert client.get("/api/slots").json()["remaining"] == 2


def test_html_form_full_ordinary_rejected_with_reason(client):
    # 先占掉唯一普通席
    gxid = publish.run_publish(device_no="DEV-BULK-01", param_no=publish.BULK_PARAM_NO)["gxid"]
    r = client.post(
        "/publish/orders",
        data={"device_no": "DEV-BULK-08", "param_no": publish.BULK_PARAM_NO, "intended_value": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "tone=danger" in loc and "GENERAL_SLOTS_FULL" in loc
    # 跟随后页面横幅给出中文拒绝原因与剩余名额
    page = client.get(loc).text if loc.startswith("http") else client.get(
        loc.replace("http://testserver", "")
    ).text
    assert "普通发布名额已满" in page and "抢修预留席" in page
    client.post(f"/publish/dba/{gxid}/rollback", follow_redirects=False)


def test_demo_emergency_end_to_end_over_http(client):
    r = client.post("/publish/demo-emergency", follow_redirects=False)
    assert r.status_code == 303 and "/publish" in r.headers["location"]
    demo = publish.LAST_DEMO
    assert demo is not None
    assert demo["rejected_ordinary"]["code"] == "GENERAL_SLOTS_FULL"
    assert demo["ordinary"]["slot_no"] == 1
    assert demo["emergency"]["slot_no"] == 2
    assert demo["emergency"]["value_before"] != demo["emergency"]["value_after"]

    page = client.get("/publish?demo=1").text
    assert "演示战报" in page and "GENERAL_SLOTS_FULL" in page

    # 普通发布仍在等锁；释放道具锁后它进入 PREPARE
    rel = client.post("/publish/blocker/release", follow_redirects=False)
    assert rel.status_code == 303
    snap = client.get("/api/slots").json()
    s1 = next(s for s in snap["slots"] if s["slot_no"] == 1)
    assert s1["phase"] == "prepared" and s1["gxid"] == demo["ordinary"]["gxid"]

    # 库端直接撤销 -> 全部还席
    rr = client.post(f"/publish/dba/{demo['ordinary']['gxid']}/rollback", follow_redirects=False)
    assert rr.status_code == 303
    assert client.get("/api/slots").json()["used"] == 0
