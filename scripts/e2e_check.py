#!/usr/bin/env python3
"""关键场景 HTTP 端到端核对（仅用标准库，可在 web 容器内/宿主机运行）。

用法：
  python scripts/e2e_check.py --base-url http://localhost:8000
  python scripts/e2e_check.py --base-url http://localhost:8000 --crash-flow

--crash-flow 会调用“收尾后、登记前立即退出”的演示开关，需要服务端
ENABLE_CRASH_DEMO=true，并且有 Docker Compose restart 策略或等价守护进程把网页拉起来。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):  # noqa: ANN001
        if response.status in (301, 302, 303, 307, 308):
            return response
        return super().http_response(request, response)

    https_response = http_response


_OPENER = urllib.request.build_opener(_NoRedirect)
_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {detail}")


def request(base: str, method: str, path: str, form: dict | None = None, timeout: int = 20):
    url = base + path
    data = None
    headers = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = _OPENER.open(req, timeout=timeout)
        body = resp.read().decode("utf-8", "replace")
        return resp.status, body, resp.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body, exc.headers.get("Location", "") if exc.headers else ""
    except Exception as exc:  # 崩溃演示时连接会直接断掉
        return 0, f"__CONN_ERROR__ {type(exc).__name__}: {exc}", ""


def follow(base: str, location: str):
    if location.startswith("http"):
        path = location[len(base):] if location.startswith(base) else location
    else:
        path = location
    return request(base, "GET", path)


def gxid_from_location(location: str) -> str:
    m = re.search(r"/txn/(GX-[A-Za-z0-9_-]+)", location)
    return m.group(1) if m else ""


def create_scenario(base: str, kind: str) -> str:
    status, _, loc = request(base, "POST", "/scenarios", {"kind": kind})
    gxid = gxid_from_location(loc)
    check(f"构造场景 {kind} -> 303/gxid", status == 303 and bool(gxid), f"status={status} loc={loc}")
    return gxid


def expect_on_txn(base: str, gxid: str, fragment: str, label: str) -> bool:
    status, body, _ = request(base, "GET", f"/txn/{gxid}")
    ok = status == 200 and fragment in body
    check(label, ok, f"status={status} 缺少片段 {fragment!r}")
    return ok


def post_action_expect_banner(base: str, gxid: str, action: str, form: dict, fragment: str, label: str):
    status, _, loc = request(base, "POST", f"/txn/{gxid}/{action}", form)
    ok_redirect = status == 303 and bool(loc)
    check(label + "（303）", ok_redirect, f"status={status} loc={loc}")
    if not ok_redirect:
        return False
    _, body, _ = follow(base, loc)
    ok = fragment in body
    check(label + "（提示文案）", ok, f"重定向页面缺少 {fragment!r}")
    return ok


def wait_health(base: str, seconds: int = 60) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, body, _ = request(base, "GET", "/healthz", timeout=5)
        if status == 200 and '"ok"' in body.replace(" ", ""):
            return True
        time.sleep(1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--crash-flow", action="store_true")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    print(f"== 目标 {base} ==")
    check("healthz 200", wait_health(base, 10))

    _, home, _ = request(base, "GET", "/")
    check("首页含内置样例 CAL-T-1001", "CAL-T-1001" in home)

    # 1) 八种现场的状态判定
    print("-- 状态判定 --")
    expected = {
        "open_no_decision": "待确认决定",
        "open_commit": "决定提交",
        "open_abort": "决定撤销",
        "committed_unregistered": "库端已提交 · 网页未登记",
        "rolledback_unregistered": "库端已回滚 · 网页未登记",
        "conflict_commit_vs_rollback": "决定提交，实际已回滚",
        "conflict_abort_vs_commit": "决定撤销，实际已提交",
        "unverified_overwrite": "证据不足",
    }
    gxids = {}
    for kind, fragment in expected.items():
        g = create_scenario(base, kind)
        gxids[kind] = g
        if g:
            expect_on_txn(base, g, fragment, f"{kind} 页面判定「{fragment}」")

    # 2) 无决定：直接 finalize 应被拒；人工补录提交后收尾；重复办理幂等
    print("-- 待确认 / 人工决定 / 幂等 --")
    g = gxids["open_no_decision"]
    status, _, loc = request(base, "POST", f"/txn/{g}/finalize", {"operator": "e2e"})
    check("无决定时直接收尾被重定向拒绝", status == 303 and "msg=" in loc)
    expect_on_txn(base, g, "待确认决定", "被拒后事务仍为待确认（锁保留）")

    post_action_expect_banner(
        base, g, "manual-decision",
        {"choice": "COMMIT", "operator": "e2e-值班员", "note": "自动化核对"},
        "人工 COMMIT 决定已补录", "人工确认提交并收尾",
    )
    expect_on_txn(base, g, "已收尾 · 已提交", "状态=已收尾·已提交")
    status, _, loc = request(base, "POST", f"/txn/{g}/finalize", {"operator": "e2e"})
    _, body, _ = follow(base, loc)
    check("重复 finalize 提示幂等且不改参数", "重复办理" in body)

    # 3) ABORT 收尾 + 重复
    print("-- 按决定回滚 --")
    g = gxids["open_abort"]
    post_action_expect_banner(base, g, "finalize", {"operator": "e2e"}, "收尾完成并已登记", "按 ABORT 决定回滚")
    expect_on_txn(base, g, "已收尾 · 已回滚", "状态=已收尾·已回滚")
    status, _, loc = request(base, "POST", f"/txn/{g}/finalize", {"operator": "e2e"})
    _, body, _ = follow(base, loc)
    check("回滚后重复办理幂等", "重复办理" in body)

    # 4) 重启证据：已提交/已回滚但未登记 -> 补登记
    print("-- 库端已收尾、网页未登记 -> 证据补登记 --")
    for kind, done_badge in (
        ("committed_unregistered", "已收尾 · 已提交"),
        ("rolledback_unregistered", "已收尾 · 已回滚"),
    ):
        g = gxids[kind]
        post_action_expect_banner(base, g, "register", {"operator": "e2e"}, "已根据数据库证据补登记", f"{kind} 补登记")
        expect_on_txn(base, g, done_badge, f"{kind} 补登记后徽章")
        status, _, loc = request(base, "POST", f"/txn/{g}/register", {"operator": "e2e"})
        _, body, _ = follow(base, loc)
        check(f"{kind} 重复补登记被忽略", "登记簿已存在" in body)

    # 5) 冲突登记
    print("-- 决定/事实冲突 --")
    for kind in ("conflict_commit_vs_rollback", "conflict_abort_vs_commit"):
        g = gxids[kind]
        post_action_expect_banner(
            base, g, "conflict",
            {"operator": "e2e", "note": "自动化冲突核对"},
            "冲突已指出并登记", f"{kind} 冲突登记",
        )
        expect_on_txn(base, g, "冲突已登记", f"{kind} 徽章=冲突已登记")

    # 6) 证据不足 -> 人工核实
    print("-- 待核实 -> 人工结论 --")
    g = gxids["unverified_overwrite"]
    post_action_expect_banner(
        base, g, "verify",
        {"claimed": "ROLLED_BACK", "operator": "e2e-专家", "note": "设备回执"},
        "人工核实结论已登记", "待核实事务人工登记",
    )
    expect_on_txn(base, g, "人工核实已登记", "徽章=人工核实已登记")

    # 7) 查询页 / 参数页 / 锁演示（空闲参数应立即拿到锁）
    print("-- 查询与锁 --")
    g = gxids["open_commit"]
    _, body, _ = request(base, "GET", f"/search?q={urllib.parse.quote(g)}")
    check("按事务号搜索命中", g in body)
    _, body, _ = request(base, "GET", "/search?q=CAL-T")
    check("按参数号搜索命中", "CAL-T-1001" in body)
    status, _, _ = request(base, "GET", "/params/CAL-T-1001")
    check("参数详情页 200", status == 200)
    status, _, loc = request(base, "POST", "/params/CAL-T-1001/lock-demo", {})
    check("空闲参数锁演示 303 且结果 acquired", status == 303 and "lock=acquired" in loc, loc)

    # 8) 真实“收尾后登记前崩溃 -> 守护重启 -> 证据恢复”
    if args.crash_flow:
        print("-- 崩溃/重启恢复（需要服务端自动重启）--")
        g = create_scenario(base, "open_commit")
        status, body, _ = request(
            base, "POST", f"/txn/{g}/finalize",
            {"operator": "e2e-crash", "crash_after_pg": "1"}, timeout=15,
        )
        check("崩溃开关使连接中断/非 303", status == 0 or status >= 500, f"status={status} {body[:120]}")
        check("服务在 60s 内被守护/Compose 重启", wait_health(base, 60))
        time.sleep(1)
        status, body, _ = request(base, "GET", f"/api/inspect/{g}")
        state = ""
        try:
            state = json.loads(body)["state"]
        except Exception:
            pass
        check("重启后凭数据库证据判定 RECONCILE_COMMIT", state == "RECONCILE_COMMIT", f"state={state} body={body[:200]}")
        status, _, loc = request(base, "POST", f"/txn/{g}/register", {"operator": "重启后值班员"})
        check("崩溃事务补登记 303", status == 303, f"status={status}")
        status, body, _ = request(base, "GET", f"/api/inspect/{g}")
        try:
            state = json.loads(body)["state"]
        except Exception:
            state = ""
        check("补登记后 DONE_COMMITTED", state == "DONE_COMMITTED", f"state={state}")

    print(f"\n结果：{_PASS} 通过，{_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
