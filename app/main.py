"""设备校准参数 · 2PC 预备事务恢复台 + 发布台名额（FastAPI）。

页面路由：
  GET  /                      总览：按号查询 + 事务队列 + 场景构造 + 名额摘要
  GET  /publish               发布台名额看板 / 抢修放行一键演示 / 库端直接收尾
  GET  /search?q=             按参数号/设备号/事务号查询
  GET  /params/{param_no}     参数详情与其发布历史
  GET  /txn/{gxid}             事务恢复页（证据、决定、锁、操作）
  POST /scenarios             构造内置中断样例
  POST /publish/orders        网页表单受理一笔发布（满额 303 回 /publish 显示原因）
  POST /publish/demo-emergency 一键“2 席下普通等锁 + 抢修预留席放行”剧本
  POST /publish/blocker/hold|release  行锁道具（制造/解除普通发布等锁）
  POST /publish/fail-demo     模拟未 PREPARE 的建单失败 -> 立即还席
  POST /publish/crash-before-prepare  模拟 PREPARE 前进程退出（需 ENABLE_CRASH_DEMO）
  POST /publish/dba/{gxid}/commit|rollback  模拟库端直接收尾（不写网页登记）
  POST /txn/{gxid}/finalize   按 SQLite 决定收尾（幂等，可带 crash_after_pg 演示）
  POST /txn/{gxid}/register   依数据库证据补登记
  POST /txn/{gxid}/manual-decision   决定缺失时值班员人工补录并收尾
  POST /txn/{gxid}/conflict    登记决定/事实冲突
  POST /txn/{gxid}/verify      证据不足时人工核实登记
  POST /params/{param_no}/lock-demo  锁阻塞演示

JSON：
  GET  /api/slots             实时名额快照（总名额/剩余/各席状态，源自 pg_locks）
  GET  /api/publish/slots     同上（别名）
  POST /api/publish/orders    JSON 受理发布；满额 409 {code,error,slots}
  POST /api/publish           同上（别名）
  GET  /api/inspect/{gxid}
  GET  /healthz
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, publish, recovery, sample, slots, sqlite_store
from .pgdb import init_postgres, wait_for_postgres

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_sqlite_dirs()
    sqlite_store.init_coord_db()
    sqlite_store.init_web_db()
    wait_for_postgres(max_wait_seconds=90)
    init_postgres()
    yield


app = FastAPI(title="校准参数发布恢复台", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _redirect_txn(gxid: str, msg: str | None = None, tone: str = "ok") -> RedirectResponse:
    url = f"/txn/{quote(gxid, safe='')}"
    if msg:
        url += f"?msg={quote(msg)}&tone={tone}"
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ 页面

@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str | None = None, tone: str = "ok") -> HTMLResponse:
    params = recovery.list_params()
    txns = recovery.list_transactions()
    open_count = sum(1 for t in txns if t["state"].startswith("OPEN_"))
    try:
        slot_snap = slots.status()
    except Exception:
        slot_snap = None
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "params": params,
            "txns": txns,
            "scenarios": sample.SCENARIOS,
            "open_count": open_count,
            "slot_snap": slot_snap,
            "crash_demo": config.ENABLE_CRASH_DEMO,
            "msg": msg,
            "tone": tone,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "") -> HTMLResponse:
    result = recovery.search(q) if q.strip() else {"params": [], "txns": []}
    return templates.TemplateResponse(
        request, "search.html", {"q": q, "result": result}
    )


@app.get("/params/{param_no:path}", response_class=HTMLResponse)
def param_page(request: Request, param_no: str, lock: str | None = None) -> HTMLResponse:
    param = recovery.get_param(param_no)
    if param is None:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"kind": "参数", "no": param_no},
            status_code=404,
        )
    # 每个发布单附上当前判定，便于展示
    enriched_orders = []
    for o in param["orders"]:
        try:
            snap = recovery.inspect(o["gxid"])
        except recovery.RecoveryError:
            snap = None
        enriched_orders.append({**o, "snap": snap})
    return templates.TemplateResponse(
        request,
        "param.html",
        {
            "param": param,
            "orders": enriched_orders,
            "lock_result": lock,
        },
    )


@app.get("/txn/{gxid:path}", response_class=HTMLResponse)
def txn_page(request: Request, gxid: str, msg: str | None = None, tone: str = "ok") -> HTMLResponse:
    try:
        snap = recovery.inspect(gxid)
    except recovery.RecoveryError as exc:
        return templates.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=400
        )
    if snap["state"] == "NOT_FOUND":
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"kind": "事务", "no": gxid},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "txn.html",
        {
            "s": snap,
            "msg": msg,
            "tone": tone,
            "crash_demo": config.ENABLE_CRASH_DEMO,
            "coord_path": config.COORD_SQLITE_PATH,
        },
    )


# ------------------------------------------------------------------ JSON API

@app.get("/api/inspect/{gxid:path}")
def api_inspect(gxid: str):
    try:
        return recovery.inspect(gxid)
    except recovery.RecoveryError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


# ------------------------------------------------------------------ 写操作

@app.post("/scenarios")
def create_scenario(kind: str = Form(...), param_no: str = Form("")):
    try:
        gxid = sample.create_scenario(kind, param_no.strip() or None)
    except Exception as exc:
        return RedirectResponse(f"/?msg={quote('场景构造失败：' + str(exc))}&tone=danger", status_code=303)
    return _redirect_txn(gxid, f"已构造样例：{sample.SCENARIOS.get(kind, kind)}", "ok")


@app.post("/txn/{gxid}/finalize")
def finalize_route(gxid: str, operator: str = Form("值班员"), crash_after_pg: str = Form("")):
    want_crash = crash_after_pg in ("1", "true", "on") and config.ENABLE_CRASH_DEMO
    try:
        result = recovery.finalize_from_decision(gxid, operator=operator, crash_after_pg=want_crash)
    except recovery.RecoveryError as exc:
        return _redirect_txn(gxid, str(exc), "danger")
    msg = "重复办理：登记簿已有结论，未再次修改参数。" if result.get("idempotent") else "收尾完成并已登记。"
    return _redirect_txn(gxid, msg, "ok")


@app.post("/txn/{gxid}/register")
def register_route(gxid: str, operator: str = Form("值班员")):
    try:
        result = recovery.register_from_evidence(gxid, operator=operator)
    except recovery.RecoveryError as exc:
        return _redirect_txn(gxid, str(exc), "danger")
    msg = "登记簿已存在，重复办理被忽略。" if result.get("idempotent") else "已根据数据库证据补登记，参数未被修改。"
    return _redirect_txn(gxid, msg, "ok")


@app.post("/txn/{gxid}/manual-decision")
def manual_decision_route(
    gxid: str,
    choice: str = Form(...),
    operator: str = Form("值班员"),
    note: str = Form(""),
    crash_after_pg: str = Form(""),
):
    want_crash = crash_after_pg in ("1", "true", "on") and config.ENABLE_CRASH_DEMO
    try:
        recovery.manual_decision_and_finalize(gxid, choice, operator, note, crash_after_pg=want_crash)
    except recovery.RecoveryError as exc:
        return _redirect_txn(gxid, str(exc), "danger")
    return _redirect_txn(gxid, f"人工 {choice} 决定已补录并完成收尾（办理人：{operator}）。", "ok")


@app.post("/txn/{gxid}/conflict")
def conflict_route(gxid: str, operator: str = Form("值班员"), note: str = Form("")):
    try:
        recovery.ack_conflict(gxid, operator, note)
    except recovery.RecoveryError as exc:
        return _redirect_txn(gxid, str(exc), "danger")
    return _redirect_txn(gxid, "冲突已指出并登记，后续请走人工补偿流程。", "warn")


@app.post("/txn/{gxid}/verify")
def verify_route(
    gxid: str,
    claimed: str = Form(...),
    operator: str = Form("值班员"),
    note: str = Form(""),
):
    try:
        recovery.manual_verify(gxid, claimed, operator, note)
    except recovery.RecoveryError as exc:
        return _redirect_txn(gxid, str(exc), "danger")
    return _redirect_txn(gxid, "人工核实结论已登记。", "warn")


@app.post("/params/{param_no:path}/lock-demo")
def lock_demo_route(param_no: str):
    result = recovery.probe_row_lock(param_no)
    flag = "blocked" if result.get("acquired") is False else ("acquired" if result.get("acquired") else "unknown")
    # 把简要结果放到 query（详细 JSON 也可在 /api 看）
    note = result.get("note", "")
    waited = result.get("waited_ms")
    lock = f"{flag}|{waited}|{note}"
    return RedirectResponse(
        f"/params/{quote(param_no, safe='')}?lock={quote(lock)}", status_code=303
    )


# ==================================================================
# 发布台名额（2 席演示：1 普通 + 1 抢修预留）
# ==================================================================

@app.get("/publish", response_class=HTMLResponse)
def publish_page(request: Request, msg: str | None = None, tone: str = "ok", code: str | None = None):
    try:
        snap = slots.status()
        cfg_check = slots.validate_total_against_pg(snap["total"])
    except Exception as exc:  # 名额配置非法时仍给出可诊断页面，而不是 500
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"发布台名额配置无效：{exc}（请检查 PUBLISH_MAX_SLOTS，须为 1..max_prepared_transactions 的整数）"},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "publish.html",
        {
            "s": snap,
            "cfg_check": cfg_check,
            "blockers": publish.blocker_status(),
            "jobs": publish.list_jobs(),
            "demo": publish.LAST_DEMO,
            "bulk_param": publish.BULK_PARAM_NO,
            "bulk_device": publish.BULK_DEVICE_NO,
            "emergency_param": publish.EMERGENCY_PARAM_NO,
            "emergency_device": publish.EMERGENCY_DEVICE_NO,
            "crash_demo": config.ENABLE_CRASH_DEMO,
            "msg": msg,
            "tone": tone,
            "code": code,
        },
    )


@app.get("/api/slots")
def api_slots():
    try:
        snap = slots.status()
        snap["config_check"] = slots.validate_total_against_pg(snap["total"])
    except Exception as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return snap


# 语义化别名，方便不同脚本/检查方式取用
@app.get("/api/publish/slots")
def api_publish_slots_alias():
    return api_slots()


@app.get("/api/quota")
def api_quota_alias():
    return api_slots()


@app.post("/publish/orders")
def publish_order_route(
    device_no: str = Form(...),
    param_no: str = Form(""),
    intended_value: str = Form(""),
    reason: str = Form(""),
):
    try:
        result = publish.run_publish(
            device_no=device_no,
            param_no=param_no.strip() or None,
            intended_value=intended_value,
            reason=reason,
        )
    except slots.SlotsFullError as exc:
        return RedirectResponse(
            f"/publish?msg={quote(exc.message)}&tone=danger&code={exc.code}", status_code=303
        )
    except (publish.PublishError, slots.SlotConfigError) as exc:
        return RedirectResponse(f"/publish?msg={quote(str(exc))}&tone=danger", status_code=303)
    kind = "抢修预留席" if result["seat_kind"] == "reserved" else "普通席"
    return _redirect_txn(
        result["gxid"],
        f"建单已受理：第 {result['slot_no']} 席（{kind}）占额，发布已进入 PREPARE；"
        "在库端提交/撤销或本页收尾前，该席一直占用。",
        "ok",
    )


async def _create_order_payload(data: dict) -> JSONResponse:
    try:
        result = await run_in_threadpool(
            publish.run_publish,
            device_no=str(data.get("device_no", "")),
            param_no=(str(data["param_no"]).strip() or None) if data.get("param_no") else None,
            intended_value=str(data.get("intended_value", "") or ""),
            reason=str(data.get("reason", "") or ""),
            lock_timeout_ms=data.get("lock_timeout_ms"),
        )
    except slots.SlotsFullError as exc:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "error": exc.message, "code": exc.code, "slots": exc.status},
        )
    except (publish.PublishError, slots.SlotConfigError) as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return JSONResponse(status_code=200, content={"ok": True, "publish": result, "slots": slots.status()})


@app.post("/api/publish/orders")
async def api_publish_order(request: Request):
    try:
        data = await request.json()
    except Exception:
        form = await request.form()
        data = dict(form)
    return await _create_order_payload(data if isinstance(data, dict) else {})


@app.post("/api/publish")
async def api_publish_alias(request: Request):
    return await api_publish_order(request)


@app.post("/api/orders")
async def api_orders_alias(request: Request):
    return await api_publish_order(request)


@app.post("/publish/demo-emergency")
def demo_emergency_route():
    try:
        summary = publish.run_emergency_demo()
        publish.LAST_DEMO = summary
    except slots.SlotsFullError as exc:
        return RedirectResponse(
            f"/publish?msg={quote(exc.message)}&tone=danger&code={exc.code}", status_code=303
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/publish?msg={quote('演示执行失败：' + type(exc).__name__ + ': ' + str(exc))}&tone=danger",
            status_code=303,
        )
    return RedirectResponse("/publish?demo=1&tone=ok", status_code=303)


@app.post("/publish/blocker/hold")
def blocker_hold_route(param_no: str = Form(publish.BULK_PARAM_NO)):
    try:
        publish.hold_row_lock(param_no.strip() or publish.BULK_PARAM_NO, key="demo-bulk")
        msg = f"已用独立会话占住 {param_no} 的参数行锁；现在普通发布会阻塞在 UPDATE 上，但仍占着普通席。"
        tone = "warn"
    except Exception as exc:  # noqa: BLE001
        msg, tone = f"加锁失败：{exc}", "danger"
    return RedirectResponse(f"/publish?msg={quote(msg)}&tone={tone}", status_code=303)


@app.post("/publish/blocker/release")
def blocker_release_route():
    ordinary_gxid = None
    if publish.LAST_DEMO:
        ordinary_gxid = publish.LAST_DEMO.get("ordinary", {}).get("gxid")
    released = publish.release_row_lock("demo-bulk")
    if not released:
        msg = "当前没有演示行锁（可能已释放或被 TTL 自动释放）。"
        tone = "info"
    else:
        msg = "行锁已释放，等待中的普通发布被唤醒。"
        tone = "ok"
    if ordinary_gxid:
        try:
            job = publish.wait_for_job(ordinary_gxid, "prepared", timeout=8)
            slot_no = (job.get("result") or {}).get("slot_no")
            msg += f" 普通发布 {ordinary_gxid} 已完成 PREPARE，仍占第 {slot_no} 席（预备态重启不释放）。"
        except Exception as exc:  # noqa: BLE001
            msg += f" 暂未观察到该普通发布进入 PREPARE：{type(exc).__name__}: {exc}"
            tone = "warn"
    return RedirectResponse(f"/publish?msg={quote(msg)}&tone={tone}", status_code=303)


@app.post("/publish/fail-demo")
def fail_demo_route(device_no: str = Form(publish.BULK_DEVICE_NO)):
    before = slots.status()
    try:
        publish.run_publish(device_no=device_no, fail_after_order=True)
    except publish.PublishError as exc:
        after = slots.status()
        msg = (
            f"已触发“建单失败（未 PREPARE）”：{exc} "
            f"名额已归还（席位事务回滚），剩余名额 {after['remaining']}/{after['total']}"
            f"（操作前剩余 {before['remaining']}/{before['total']}）。"
        )
        return RedirectResponse(f"/publish?msg={quote(msg)}&tone=warn", status_code=303)
    return RedirectResponse("/publish?msg=未按预期失败&tone=danger", status_code=303)


@app.post("/publish/crash-before-prepare")
def crash_before_prepare_route(device_no: str = Form(publish.BULK_DEVICE_NO)):
    if not config.ENABLE_CRASH_DEMO:
        return RedirectResponse(
            "/publish?msg=" + quote("崩溃演示未启用（ENABLE_CRASH_DEMO!=true）") + "&tone=danger",
            status_code=303,
        )
    # os._exit(43) 发生在 run_publish 内部、PREPARE 之前；Compose 会重启网页。
    publish.run_publish(device_no=device_no, crash_before_prepare=True)
    return RedirectResponse("/publish", status_code=303)


@app.post("/publish/dba/{gxid}/commit")
def dba_commit_route(gxid: str):
    return _dba_redirect(gxid, "COMMIT")


@app.post("/publish/dba/{gxid}/rollback")
def dba_rollback_route(gxid: str):
    return _dba_redirect(gxid, "ROLLBACK")


def _dba_redirect(gxid: str, choice: str) -> RedirectResponse:
    try:
        out = publish.dba_direct_prepared(gxid, choice)
    except recovery.RecoveryError as exc:
        return RedirectResponse(f"/publish?msg={quote(str(exc))}&tone=danger", status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/publish?msg={quote('库端直接收尾失败：' + type(exc).__name__ + ': ' + str(exc))}&tone=danger",
            status_code=303,
        )
    if out["executed"]:
        msg = (
            f"已在库端直接执行 {choice} PREPARED '{gxid}'，PostgreSQL 立即释放席位；"
            "未写网页登记簿（无需等网页补登记）。可到事务页按证据补登记。"
        )
        tone = "ok"
    else:
        msg = f"该事务已不在 pg_prepared_xacts 中，无需 {choice}；席位锁应已释放。"
        tone = "info"
    if out["slot_still_occupied"]:
        msg += " 注意：名额看板仍显示该号占席，请刷新核对 pg_locks。"
        tone = "warn"
    return RedirectResponse(f"/publish?msg={quote(msg)}&tone={tone}", status_code=303)
