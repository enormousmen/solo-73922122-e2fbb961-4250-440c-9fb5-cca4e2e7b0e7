"""设备校准参数 · 2PC 预备事务恢复页（FastAPI）。

页面路由：
  GET  /                      总览：按号查询 + 事务队列 + 场景构造
  GET  /search?q=             按参数号/设备号/事务号查询
  GET  /params/{param_no}     参数详情与其发布历史
  GET  /txn/{gxid}             事务恢复页（证据、决定、锁、操作）
  POST /scenarios             构造内置中断样例
  POST /txn/{gxid}/finalize   按 SQLite 决定收尾（幂等，可带 crash_after_pg 演示）
  POST /txn/{gxid}/register   依数据库证据补登记
  POST /txn/{gxid}/manual-decision   决定缺失时值班员人工补录并收尾
  POST /txn/{gxid}/conflict    登记决定/事实冲突
  POST /txn/{gxid}/verify      证据不足时人工核实登记
  POST /params/{param_no}/lock-demo  锁阻塞演示

JSON：
  GET  /api/inspect/{gxid}
  GET  /healthz
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, recovery, sample, sqlite_store
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
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "params": params,
            "txns": txns,
            "scenarios": sample.SCENARIOS,
            "open_count": open_count,
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
