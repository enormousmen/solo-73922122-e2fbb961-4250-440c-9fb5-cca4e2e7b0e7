#!/usr/bin/env python3
"""发布台名额 / 抢修放行 关键场景 HTTP 核对（仅标准库）。

用法：
  python scripts/slot_check.py --base-url http://localhost:8000

覆盖：
  1. /api/slots 暴露 2 席（1 普通 + 1 抢修预留）与剩余名额；/publish 页面可渲染。
  2. 一键抢修演示：普通发布占普通席并等锁 -> 第二个普通建单被拒（原因码
     GENERAL_SLOTS_FULL）-> 抢修设备用预留席放行并更新参数。
  3. 释放行锁后普通发布进入 PREPARE（仍占席）。
  4. 库端直接 ROLLBACK PREPARED（网页 DBA 代理接口，不写登记簿）后名额自动归还。
  5. 建单失败（未 PREPARE）立即还席。
  6. JSON 接口的并发满额 409、预留席与库端收尾还席。

“网页重启后预备态仍占额 / 预备前进程退出还席”属于进程级场景，
由 tests/test_slots.py 与交付说明中的专项脚本/命令覆盖。
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


def request(base: str, method: str, path: str, form: dict | None = None,
            body: dict | None = None, timeout: int = 30):
    url = base + path
    data = None
    headers = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = _OPENER.open(req, timeout=timeout)
        raw = resp.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return resp.status, raw, resp.headers.get("Location", ""), parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        return exc.code, raw, exc.headers.get("Location", "") if exc.headers else "", parsed
    except Exception as exc:  # 崩溃/连接中断
        return 0, f"__CONN_ERROR__ {type(exc).__name__}: {exc}", "", None


def wait_slots(base: str, predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        st, _, _, js = request(base, "GET", "/api/slots")
        if st == 200 and js:
            last = js
            if predicate(js):
                return js
        time.sleep(0.25)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    # 0) 健康
    st, raw, _, _ = request(base, "GET", "/healthz")
    check("healthz 200", st == 200 and '"ok"' in raw.replace(" ", ""), f"status={st}")

    # 1) 名额初始布局（若上一轮有残留，先尝试通过页面把 GX 预备事务撤销干净）
    snap = wait_slots(base, lambda s: s.get("total") == 2, timeout=15)
    check("/api/slots total=2", bool(snap and snap.get("total") == 2), str(snap)[:200])
    if snap:
        check("1 个普通席", snap.get("general_total") == 1)
        check("第 2 席为预留席", snap.get("reserved_slot_no") == 2 and snap.get("reserved_total") == 1)
        check("指定抢修设备 DEV-EMG-01", "DEV-EMG-01" in snap.get("emergency_devices", []))

    st, body, _, _ = request(base, "GET", "/publish")
    check("/publish 页面 200 且含关键文案",
          st == 200 and "抢修预留席" in body and "剩余名额" in body, f"status={st}")

    # 2) 一键抢修演示
    st, _, loc, _ = request(base, "POST", "/publish/demo-emergency", form={})
    check("POST /publish/demo-emergency -> 303", st == 303 and "/publish" in loc, f"status={st} loc={loc}")

    st, page, _, _ = request(base, "GET", "/publish?demo=1")
    check("演示战报含拒绝原因码 GENERAL_SLOTS_FULL", "GENERAL_SLOTS_FULL" in page)
    check("演示战报含抢修设备与第 2 席", "DEV-EMG-01" in page and "第 2 席" in page)

    snap = wait_slots(
        base,
        lambda s: any(x.get("phase") == "waiting_lock" and x.get("kind") == "general"
                      for x in s.get("slots", [])),
        timeout=8,
    )
    waiter = next((x for x in (snap or {}).get("slots", []) if x.get("phase") == "waiting_lock"), None)
    check("看板观察到普通席‘等参数行锁仍占席’", waiter is not None, str(snap)[:300])
    ordinary_gxid = waiter.get("gxid") if waiter else None

    # 3) 释放行锁 -> 普通发布进入 PREPARE
    st, _, loc, _ = request(base, "POST", "/publish/blocker/release", form={})
    check("释放行锁 -> 303", st == 303)
    snap = wait_slots(
        base,
        lambda s: any(x.get("gxid") == ordinary_gxid and x.get("phase") == "prepared"
                      for x in s.get("slots", [])),
        timeout=12,
    )
    prep = next((x for x in (snap or {}).get("slots", []) if x.get("gxid") == ordinary_gxid), None)
    check("普通发布被唤醒并进入 PREPARE（仍占席）", bool(prep and prep["phase"] == "prepared"),
          str(prep)[:200])

    # 4) 库端直接撤销 -> 还席（不写网页登记）
    if ordinary_gxid:
        st, _, _, _ = request(base, "POST", f"/publish/dba/{ordinary_gxid}/rollback", form={})
        check("DBA 直接 ROLLBACK PREPARED -> 303", st == 303, f"status={st}")
        snap = wait_slots(base, lambda s: s.get("used") == 0, timeout=8)
        check("库端收尾后名额全部归还", bool(snap and snap.get("used") == 0), str(snap)[:200])

    # 5) 建单失败还席
    before = (snap or {}).get("remaining", 2)
    st, _, fail_loc, _ = request(base, "POST", "/publish/fail-demo",
                                 form={"device_no": "DEV-BULK-01"})
    check("fail-demo -> 303", st == 303, f"status={st}")
    if fail_loc:
        path = fail_loc if fail_loc.startswith("/") else "/" + fail_loc.split(base, 1)[-1]
        _, fail_page, _, _ = request(base, "GET", path)
        check("失败提示含‘名额已归还’", "名额已归还" in fail_page)
    snap = wait_slots(base, lambda s: s.get("remaining") == before, timeout=6)
    check("建单失败（未 PREPARE）后名额归还", bool(snap and snap.get("remaining") == 2), str(snap)[:160])

    # 6) JSON 满额/预留/收尾
    st, _, _, js = request(base, "POST", "/api/publish/orders",
                           body={"device_no": "DEV-BULK-01", "param_no": "CAL-BULK-1001"})
    check("JSON 普通受理 -> 200 占 1 席", st == 200 and js and js["publish"]["slot_no"] == 1, f"status={st}")
    g1 = js["publish"]["gxid"] if js else None

    st, _, _, js2 = request(base, "POST", "/api/publish/orders",
                            body={"device_no": "DEV-BULK-02", "param_no": "CAL-BULK-1001"})
    check("并发第二笔普通 -> 409 GENERAL_SLOTS_FULL",
          st == 409 and js2 and js2.get("code") == "GENERAL_SLOTS_FULL", f"status={st} body={str(js2)[:160]}")
    check("409 体给出中文拒绝原因", bool(js2 and "预留" in js2.get("error", "")))

    st, _, _, js3 = request(base, "POST", "/api/publish/orders",
                            body={"device_no": "DEV-EMG-01", "param_no": "CAL-EMRG-0001"})
    check("抢修设备 -> 200 使用第 2 预留席",
          st == 200 and js3 and js3["publish"]["slot_no"] == 2, f"status={st} body={str(js3)[:160]}")
    g2 = js3["publish"]["gxid"] if js3 else None

    st, _, _, js4 = request(base, "POST", "/api/publish/orders",
                            body={"device_no": "DEV-EMG-01", "param_no": "CAL-EMRG-0001"})
    check("两席全占后抢修也 409 ALL_SLOTS_FULL",
          st == 409 and js4 and js4.get("code") == "ALL_SLOTS_FULL", f"status={st}")

    for g in (g1, g2):
        if g:
            request(base, "POST", f"/publish/dba/{g}/rollback", form={})
    snap = wait_slots(base, lambda s: s.get("used") == 0, timeout=8)
    check("JSON 两笔库端撤销后名额清零", bool(snap and snap.get("used") == 0), str(snap)[:160])

    print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
