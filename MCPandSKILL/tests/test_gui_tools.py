"""Live checks for the GUI tools (needs a running gui-mode ANSA).

Run with the MCP server's venv:

    .venv/Scripts/python.exe tests/test_gui_tools.py

It talks to whatever ANSA .ansa_mcp_state.json points at, so start one first
(ansa_launch) or attach to an existing ``-listenport`` session.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ansa_mcp_server as S  # noqa: E402

OUT = S.SERVER_DIR / "out"
OUT.mkdir(exist_ok=True)


def _run(code: str, **kw):
    return S.SESSION.run(code, tool="test_gui_tools", **kw)


def check_window_shot() -> bool:
    target = OUT / "gui_tool_window.png"
    target.unlink(missing_ok=True)
    res = _run(S._window_shot_code(target, "png"))
    ok = bool(res.get("ok")) and target.exists()
    print(f"[window shot] ok={res.get('ok')} size={target.stat().st_size if ok else 0}")
    if res.get("result"):
        print("  result:", res["result"][:400])
    if not ok:
        print("  error:", (res.get("error") or "")[:800])
    return ok


def check_gui_state() -> bool:
    res = _run(r'''
_state = {}
try:
    _state["gui_mode"] = bool(guitk.BCApplicationIsGuiMode())
except Exception as _exc:
    _state["gui_mode"] = "ERR: %s" % _exc
try:
    _state["menu"] = base.CurrentMenu()
except Exception as _exc:
    _state["menu"] = "ERR: %s" % _exc
_state
''')
    ok = bool(res.get("ok"))
    print(f"[gui state] ok={ok} result={res.get('result')}")
    return ok


def check_menu_switch() -> bool:
    before = _run("base.CurrentMenu()").get("result", "")
    res = _run("""
_r = base.SetCurrentMenu('MESH')
try:
    guitk.BCApplicationProcessNonUserInputEvents()
except Exception:
    pass
{'ret': _r, 'menu': base.CurrentMenu()}
""")
    data = json.loads(res.get("result") or "{}")
    print(f"[menu switch] {before} -> {data.get('menu')} (ret={data.get('ret')})")
    # Put it back so the test leaves no trace.
    _run("base.SetCurrentMenu('TOPO')")
    return data.get("menu") != before


def check_async() -> bool:
    reply = S.ansa_run_async("""
import time
total = 0
for i in range(3):
    print('tick', i)
    pump()
    total += i
    time.sleep(0.4)
print('done, total =', total)
""")
    head = (reply or "").splitlines()[0] if reply else ""
    print(f"[async dispatch] {head}")
    match = re.search(r"job_\d{8}_\d{6}_[0-9a-z]+", reply or "")
    if not match:
        print("  no job id in reply:", (reply or "")[:300])
        return False
    job_id = match.group(0)

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            data = json.loads((S.JOBS_DIR / f"{job_id}.json").read_text("utf-8"))
        except Exception:
            data = {}
        if data.get("status") in ("ok", "error"):
            log = (S.JOBS_DIR / f"{job_id}.log")
            text = log.read_text("utf-8", errors="replace") if log.exists() else ""
            print(f"[async finish] status={data['status']} lines={len(text.splitlines())}")
            print("  log:", text.strip().replace("\n", " | ")[:200])
            return data["status"] == "ok" and "total = 3" in text
        time.sleep(0.4)
    print("[async finish] TIMEOUT")
    return False


def main() -> int:
    # Boot a dedicated session: IAP accepts one client, so a test process
    # cannot share the connection the MCP server already holds.
    info = S.SESSION.launch(mode="gui", wait=240.0)
    print(f"ANSA gui on port {info['port']} "
          f"({info.get('boot_seconds')}s, pid {info.get('pid')})\n")

    results = {
        "window_shot": check_window_shot(),
        "gui_state": check_gui_state(),
        "menu_switch": check_menu_switch(),
        "async": check_async(),
    }
    print()
    for name, ok in results.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
