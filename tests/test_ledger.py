"""Offline tests for the idempotency ledger and the OUTCOME_UNKNOWN path.

These never launch ANSA and never consume a licence: the fake IAP client below
reproduces the only thing that matters here -- *why* a call failed.

Run with the interpreter that has ``mcp`` installed::

    python tests/test_ledger.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ansa_iap import IapError, IapOutcomeUnknown, ScriptResult  # noqa: E402
from ansa_ledger import (  # noqa: E402
    FAILED,
    SUCCESS,
    UNKNOWN,
    LedgerFull,
    OperationConflict,
    OperationLedger,
    OperationOutcomeUnknown,
    fingerprint,
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  <- {detail}" if detail else ""))


# --------------------------------------------------------------- ledger ----
def test_ledger() -> None:
    print("\nledger")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.json"
        led = OperationLedger(path)
        fp = fingerprint("ansa_mesh", {"entities": "PSHELL"})

        check("fresh reserve returns None", led.reserve("op_a", "ansa_mesh", fp) is None)
        check("record is pending", led.get("op_a")["status"] == "pending")

        led.finish("op_a", SUCCESS, summary="meshed 3 props", elapsed=12.5)
        replay = led.reserve("op_a", "ansa_mesh", fp)
        check("successful op replays", replay is not None)
        check("replay carries the summary",
              replay is not None and replay["summary"] == "meshed 3 props")

        try:
            led.reserve("op_a", "ansa_mesh", fingerprint("ansa_mesh", {"entities": "PSOLID"}))
            check("param change is refused", False)
        except OperationConflict:
            check("param change is refused", True)

        led.reserve("op_b", "ansa_mesh", fp)
        try:
            led.reserve("op_b", "ansa_mesh", fp)
            check("pending retry is refused", False)
        except OperationOutcomeUnknown:
            check("pending retry is refused", True)

        led.finish("op_b", UNKNOWN, error="timeout")
        try:
            led.reserve("op_b", "ansa_mesh", fp)
            check("unknown retry is refused", False)
        except OperationOutcomeUnknown:
            check("unknown retry is refused", True)

        led.reserve("op_c", "ansa_mesh", fp)
        led.finish("op_c", FAILED, error="script raised")
        check("failed op may be retried",
              led.reserve("op_c", "ansa_mesh", fp) is None)

        reloaded = OperationLedger(path)
        check("records survive a server restart", reloaded.get("op_a")["status"] == "success")

        tiny = OperationLedger(Path(tmp) / "small.json", max_records=1)
        tiny.reserve("op_x", "t", fp)
        tiny.finish("op_x", SUCCESS)
        try:
            tiny.reserve("op_y", "t", fp)
            check("full ledger refuses new writes", False)
        except LedgerFull:
            check("full ledger refuses new writes", True)


# ------------------------------------------------------------ session run ----
class FakeClient:
    """Mimics IapClient.execute just closely enough for AnsaSession.run."""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.calls = 0

    def execute(self, script, entry=None, keep_database=True, timeout=None):
        self.calls += 1
        if self.behaviour == "unknown":
            raise IapOutcomeUnknown("timed out waiting for ANSA after 600s")
        if self.behaviour == "dead":
            raise IapError("cannot connect to ANSA")
        res = ScriptResult(result_code=0, exec_details=0, elapsed=0.5)
        if self.behaviour == "script_error":
            res.payload = {"ok": "0", "error": "ValueError: boom", "stdout": ""}
        else:
            res.payload = {"ok": "1", "result": "{}", "stdout": "", "elapsed": "0.50"}
        return res


def test_session() -> None:
    print("\nsession.run")
    import ansa_mcp_server as srv

    tmp = Path(tempfile.mkdtemp()) / "ledger.json"
    srv.LEDGER = OperationLedger(tmp)

    sess = srv.AnsaSession()
    sess.client = FakeClient("ok")
    # No ANSA may be launched or contacted from a unit test.
    sess._counts = lambda: None
    sess._load_state = lambda: {}
    sess.port = None

    first = sess.run("1", mutating=True, measure=False)
    check("successful write returns OK", first["status"] == "OK")
    op_id = first["operation_id"]
    check("operation_id is returned", op_id.startswith("op_"))

    calls = sess.client.calls
    replay = sess.run("1", mutating=True, measure=False, operation_id=op_id)
    check("same id replays instead of re-running", replay["status"] == "REPLAYED")
    check("ANSA was not called for the replay", sess.client.calls == calls)

    sess.client = FakeClient("unknown")
    unknown = sess.run("2", mutating=True, measure=False)
    check("timeout is OUTCOME_UNKNOWN", unknown["status"] == "OUTCOME_UNKNOWN")
    check("unknown is not retryable", unknown["retryable"] is False)

    blocked = sess.run("2", mutating=True, measure=False,
                       operation_id=unknown["operation_id"])
    check("retrying an unknown op is refused", blocked["status"] == "OUTCOME_UNKNOWN")
    check("refused op reports the ledger phase", blocked.get("phase") == "ledger")

    sess.client = FakeClient("dead")
    dead = sess.run("3", mutating=True, measure=False)
    check("dead socket is TRANSPORT_FAILED", dead["status"] == "TRANSPORT_FAILED")
    check("transport failure is retryable", dead.get("retryable") is True)

    try:
        sess.ensure(allow_launch=False)
        check("retry never launches a fresh ANSA", False)
    except IapError:
        check("retry never launches a fresh ANSA", True)

    sess.client = FakeClient("script_error")
    bad = sess.run("4", mutating=True, measure=False)
    check("script exception is SCRIPT_ERROR", bad["status"] == "SCRIPT_ERROR")

    # A read-only call must never touch the ledger.
    before = srv.LEDGER.size()
    sess.client = FakeClient("ok")
    sess.run("5", mutating=False, measure=False)
    check("read-only calls leave the ledger alone", srv.LEDGER.size() == before)

    hist = srv.STEPS
    check("steps are recorded", len(hist) >= 4)
    check("unresolved step is in the history",
          any(s["status"] == "OUTCOME_UNKNOWN" for s in hist))


def test_strict_bool() -> None:
    print("\nstrict bool")
    import ansa_mcp_server as srv

    check("True passes", srv._strict_bool(True, "x")[0] is True)
    check("False passes", srv._strict_bool(False, "x")[0] is False)
    check("1 is rejected", srv._strict_bool(1, "x")[0] is None)
    check("'true' is rejected", srv._strict_bool("true", "x")[0] is None)


def test_formatter() -> None:
    print("\nformatter")
    import ansa_mcp_server as srv

    unknown = srv._fmt({"ok": False, "status": "OUTCOME_UNKNOWN",
                        "error": "timeout", "operation_id": "op_x",
                        "mutating": True})
    check("unknown text warns about the model", "may already have been changed" in unknown)
    check("unknown text says not to use a new id", "fresh operation_id" in unknown)

    ok = srv._fmt({"ok": True, "status": "OK", "operation_id": "op_y",
                   "elapsed": "1.0", "delta": {"SHELL": 6341}, "mutating": True})
    check("evidence line separates layers", "engineering=unverified" in ok)
    check("delta is shown", "SHELL +6341" in ok)

    ro = srv._fmt({"ok": True, "status": "OK", "operation_id": "op_z",
                   "elapsed": "0.1", "mutating": False})
    check("read-only calls say model is untracked", "not tracked" in ro)

    nm = srv._fmt({"ok": True, "status": "OK", "operation_id": "op_n",
                   "elapsed": "0.2", "delta": {}, "mutating": True,
                   "measured": False})
    check("unmeasured writes do not claim 'unchanged'", "not measured" in nm)


def main() -> int:
    test_ledger()
    test_session()
    test_strict_bool()
    test_formatter()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
