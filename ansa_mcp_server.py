"""ANSA MCP server - drive BETA CAE ANSA from an MCP client.

Transport
---------
MCP tools are served over stdio. The actual work happens inside a real ANSA
process, reached through ANSA's official *Inter ANSA Protocol* (IAP):
ANSA is started with ``-listenport <port>`` and the server sends it python
scripts to execute.

Design notes
------------
* ANSA opens the listen socket several seconds before the IAP handler is
  ready, so the handshake is retried (see ``ansa_iap.connect_with_retry``).
* Script output is captured by injecting a private ``print`` into the
  execution namespace: ANSA routes ``sys.stdout`` to its own console, so
  redirecting ``sys.stdout`` does not work.
* A python dict kept in ``sys.modules['ansa_mcp_runtime']`` survives across
  calls, exposed to user code as ``ansavars`` - handy for keeping entity
  handles between steps.
* Every call is a fresh script, but the ANSA database persists.
* The ANSA child process gets its own log file for stdout/stderr so that
  nothing can corrupt the MCP stdio channel.
* Mutating tools carry an ``operation_id`` and are recorded in
  :mod:`ansa_ledger` *before* the script is sent. A timeout would otherwise be
  indistinguishable from a clean failure, and a retry would apply the change
  twice. Unresolved calls are reported as ``OUTCOME_UNKNOWN`` instead.
* Results separate three different kinds of evidence -- transport, script
  return, model change -- because "the API did not raise" is not the same as
  "the model is correct".
"""
from __future__ import annotations

import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from mcp.server.fastmcp import FastMCP

from ansa_iap import IapClient, IapError, IapOutcomeUnknown, connect_with_retry
from ansa_ledger import (
    FAILED,
    SUCCESS,
    UNKNOWN,
    LedgerError,
    OperationLedger,
    OperationOutcomeUnknown,
    fingerprint,
    new_operation_id,
)

# --------------------------------------------------------------- constants ---
SERVER_NAME = "ansa"
SERVER_DIR = Path(__file__).resolve().parent
STATE_FILE = SERVER_DIR / ".ansa_mcp_state.json"
LOG_DIR = SERVER_DIR / "logs"

# Only a fallback: the MCP client config is expected to set ANSA_HOME, because
# the install root differs per site (drive letter, release, network share).
DEFAULT_ANSA_HOME = Path(r"C:\Program Files\BETA_CAE_Systems\ansa_v23.0.0")
ANSA_HOME = Path(os.environ.get("ANSA_HOME", DEFAULT_ANSA_HOME))
RESULT_LIMIT = int(os.environ.get("ANSA_MCP_RESULT_LIMIT", "120000"))
DEFAULT_TIMEOUT = float(os.environ.get("ANSA_MCP_TIMEOUT", "600"))

# Optional containment for every path the server writes to. Unset keeps the
# historical "write anywhere" behaviour; set it to make exports predictable.
WORKSPACE = (os.environ.get("ANSA_MCP_WORKSPACE") or "").strip()

LEDGER = OperationLedger(SERVER_DIR / ".ansa_mcp_ledger.json")

# Session-local step history: what the agent did, in what order, and whether
# anything observable changed. Not a durable audit log.
STEPS: deque[dict[str, Any]] = deque(maxlen=100)
_STEP_SEQ = itertools.count(1)

# Entity types cheap enough to count around every mutating call.
COUNT_TYPES = ("GRID", "SHELL", "SOLID", "BEAM", "PART",
               "__PROPERTIES__", "__MATERIALS__")


DECK_OUTPUT = {
    "nastran": "OutputNastran",
    "abaqus": "OutputAbaqus",
    "ansys": "OutputAnsys",
    "lsdyna": "OutputLSDyna",
    "optistruct": "OutputOptistruct",
    "pamcrash": "OutputPamCrash",
    "radioss": "OutputRadioss",
    "marc": "OutputMarc",
    "ideas": "OutputIdeasV",
    "patran": "OutputPatran",
    "stl": "OutputStereoLithography",
    "cgns": "OutputCGNS",
    "vrml": "OutputVrml",
    "permas": "OutputPermas",
    "sestra": "OutputSestra",
}

# Solver input files go through base.Input<Deck>, NOT base.Open.
# base.Open only understands CAD formats (and .ansa sessions); handing it a
# .nas file fails with "Not a valid CAD file !!".
INPUT_ROUTES: dict[str, tuple[str, set[str]]] = {
    "nastran": ("InputNastran", {".nas", ".bdf", ".dat", ".nastran", ".blk"}),
    "abaqus": ("InputAbaqus", {".inp", ".abq", ".inc"}),
    "ansys": ("InputAnsys", {".cdb", ".ans"}),
    "lsdyna": ("InputLSDyna", {".key", ".k", ".dyna"}),
    "pamcrash": ("InputPamCrash", {".pc"}),
    "radioss": ("InputRadioss", {".rad", ".0000", ".0001"}),
    "patran": ("InputPatran", {".pat", ".patran", ".neu"}),
    "permas": ("InputPermas", {".permas"}),
    "stl": ("InputStereoLithography", {".stl"}),
    "vrml": ("InputVrml", {".wrl", ".vrml"}),
    "cgns": ("InputCGNS", {".cgns"}),
    "ugrid": ("InputUh3d", {".ugrid"}),
    "openfoam": ("InputOpenFoam", {".foam"}),
}

_EXT_TO_DECK = {ext: name for name, (_, exts) in INPUT_ROUTES.items() for ext in exts}


def _route_input(ext: str, file_format: str) -> tuple[str | None, str]:
    """Return (func_name_or_None_for_base_Open, resolved_format)."""
    fmt = (file_format or "auto").strip().lower()
    if fmt in ("auto", ""):
        deck = _EXT_TO_DECK.get(ext)
        if deck:
            return INPUT_ROUTES[deck][0], deck
        return None, "cad"
    if fmt == "cad":
        return None, "cad"
    if fmt not in INPUT_ROUTES:
        raise ValueError(f"unknown file_format `{file_format}`; use auto/cad or one of: "
                         + ", ".join(sorted(INPUT_ROUTES)))
    return INPUT_ROUTES[fmt][0], fmt

INSTRUCTIONS = """\
This server drives BETA CAE ANSA (CAE pre-processor) through its official
Inter ANSA Protocol. Each tool call executes a real python script INSIDE a
running ANSA process, so the ANSA model database persists between calls.

Typical workflow
1. `ansa_status` - see whether an ANSA instance is live.
2. `ansa_open(path)` - import step/igs/nas/ansa/cad geometry.
3. `ansa_model_info` - check what landed in the database.
4. `ansa_run_python(code)` - do the real work: filtering, properties,
   materials, transforms, batch mesh, includes...
5. `ansa_screenshot(path)` - visual check (works best in gui mode).
6. `ansa_export(path, deck)` - write solver input.
7. `ansa_save(path)` - write a .ansa session file.

Inside `ansa_run_python` these names are pre-imported and always available:
  ansa, base, constants, session, mesh, batchmesh, utils, morph, guitk
  ansavars  - a persistent dict shared by every call
  print(...) - captured and returned in the tool result

The last expression of the snippet becomes the returned `result`, Jupyter
style. Return JSON-able data (ids, counts, lists of dicts), not Entity
objects - use `base.GetEntityType(deck, e)` / `e._id` / `e._name` instead.

Notes
- ANSA's python bindings have no docstrings; use `ansa_api_search` to look
  up exact signatures before calling an unfamiliar function.
- Deferred/expensive operations (batch mesh, output) can take minutes; raise
  `timeout` accordingly.

Retrying safely
Every mutating tool returns an `operation_id`. If a call ends with
OUTCOME_UNKNOWN, ANSA may have already applied it and may still be running:
read the model back (`ansa_model_info`) instead of resending the script.
If you do need to retry, pass the SAME `operation_id` - the server replays the
recorded result instead of running the change twice. Reusing an id for a
different operation is refused.
"""

mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)


# ------------------------------------------------------------------ helpers ---
def _log(msg: str) -> None:
    print(f"[ansa-mcp] {msg}", file=sys.stderr, flush=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def _py_literal(code: str) -> str:
    """Embed arbitrary user code safely as a python string literal."""
    return repr(code)


_SCRIPT_TEMPLATE = r'''
def main():
    import sys as _sys, json as _json, traceback as _tb
    import types as _types, ast as _ast, time as _time

    _out = []
    def _emit(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        if sep is None:
            sep = " "
        text = sep.join(str(a) for a in args)
        if end and end != "\n":
            text += end
        _out.append(text)

    _rt = _sys.modules.get("ansa_mcp_runtime")
    if _rt is None:
        _rt = _types.ModuleType("ansa_mcp_runtime")
        _rt.vars = {}
        _sys.modules["ansa_mcp_runtime"] = _rt

    _ns = {"__name__": "__main__", "print": _emit, "ansavars": _rt.vars}

    _import_err = ""
    try:
        import ansa as _ansa
        from ansa import base as _b, constants as _c, session as _s
        _ns.update(ansa=_ansa, base=_b, constants=_c, session=_s)
        for _m in ("mesh", "batchmesh", "utils", "morph", "cad", "dm",
                   "connections", "kinetics", "calc", "taskmanager",
                   "betascript", "script", "guitk", "spdrm"):
            try:
                _ns[_m] = getattr(_ansa, _m)
            except Exception:
                pass
    except Exception:
        _import_err = _tb.format_exc()

    _ok, _err, _result = True, "", None
    _t0 = _time.time()
    try:
        _tree = _ast.parse(__ANSA_MCP_CODE__)
        _tail = None
        if _tree.body and isinstance(_tree.body[-1], _ast.Expr):
            _tail = _tree.body.pop()
        exec(compile(_tree, "<ansa-mcp>", "exec"), _ns)
        if _tail is not None:
            _result = eval(compile(_ast.Expression(_tail.value), "<ansa-mcp>", "eval"), _ns)
    except BaseException:
        _ok = False
        _err = _tb.format_exc()

    def _encode(value, limit):
        if value is None:
            return ""
        try:
            text = _json.dumps(value, default=repr, ensure_ascii=False)
        except Exception:
            try:
                text = repr(value)
            except Exception:
                text = "<unserializable>"
        if len(text) > limit:
            text = text[:limit] + "\n... <truncated, total length {}>".format(len(text))
        return text

    def _raw(text, limit):
        if len(text) > limit:
            text = text[:limit] + "\n... <truncated, total length {}>".format(len(text))
        return text

    return {
        "ok": "1" if _ok else "0",
        "stdout": _raw("\n".join(_out), __ANSA_MCP_LIMIT__),
        "result": _encode(_result, __ANSA_MCP_LIMIT__),
        "error": _err[:20000],
        "elapsed": "{:.2f}".format(_time.time() - _t0),
        "import_error": _import_err[:4000],
    }
'''


def _build_script(code: str, limit: int = RESULT_LIMIT) -> str:
    return (_SCRIPT_TEMPLATE
            .replace("__ANSA_MCP_CODE__", _py_literal(code))
            .replace("__ANSA_MCP_LIMIT__", str(int(limit))))


# ----------------------------------------------------------- session state ---
class AnsaSession:
    """Owns at most one ANSA process plus its IAP connection."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.client: IapClient | None = None
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.mode: str | None = None
        self.exe_args: list[str] = []
        self.started_at: float | None = None
        self.last_error: str = ""
        # ANSA's own pid, read from inside the process. ``proc.pid`` is the
        # cmd.exe launcher wrapper, so it cannot be used to identify ANSA.
        self.ansa_pid: int | None = None

    # -- state file --------------------------------------------------------
    def _save_state(self) -> None:
        try:
            STATE_FILE.write_text(json.dumps({
                "port": self.port,
                "pid": self.proc.pid if self.proc else None,
                "ansa_pid": self.ansa_pid,
                "mode": self.mode,
                "started_at": self.started_at,
            }, indent=2), encoding="utf-8")
        except OSError as exc:
            _log(f"cannot write state file: {exc}")

    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def clear_state(self) -> None:
        try:
            STATE_FILE.unlink()
        except OSError:
            pass

    # -- process -----------------------------------------------------------
    @property
    def ansa_bat(self) -> Path:
        for name in ("ansa64.bat", "ansa.bat"):
            candidate = ANSA_HOME / name
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"ansa64.bat not found under {ANSA_HOME}. Set the ANSA_HOME "
            "environment variable to your ANSA installation folder."
        )

    def launch(self, mode: str = "gui", port: int | None = None,
               extra_args: Iterable[str] | None = None,
               wait: float = 240.0) -> dict:
        with self._lock:
            if self.client is not None and self._alive():
                return {"already_running": True, "port": self.port, "mode": self.mode}
            self._kill_orphans()
            port = port or _free_port()
            bat = self.ansa_bat
            args = ["-nolauncher", "-listenport", str(port), "-foregr"]
            if mode == "batch":
                args.append("-b")
            if extra_args:
                args.extend(str(a) for a in extra_args)
            cmd = ["cmd", "/c", str(bat), *args]

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(LOG_DIR / f"ansa_{port}.log", "w",
                                encoding="utf-8", errors="replace")
            _log(f"launching ANSA ({mode}) on port {port}")
            t0 = time.time()
            # ANSA must outlive the process that started it: an MCP client
            # restart, or a shell/job object tearing down its children, must
            # not take the user's model down with it. CREATE_BREAKAWAY_FROM_JOB
            # detaches it from the enclosing job; if the job forbids that, fall
            # back to a plain new process group.
            base_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            breakaway = (getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
                         | base_flags)
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=str(ANSA_HOME),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_fh, stderr=subprocess.STDOUT,
                    creationflags=breakaway,
                )
            except OSError:
                self.proc = subprocess.Popen(
                    cmd, cwd=str(ANSA_HOME),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_fh, stderr=subprocess.STDOUT,
                    creationflags=base_flags,
                )
            self.port = port
            self.mode = mode
            self.exe_args = args
            self.started_at = time.time()
            self.client = connect_with_retry(port, timeout=DEFAULT_TIMEOUT,
                                            deadline=wait, quiet=True)
            self.ansa_pid = self._probe_pid()
            self._save_state()
            return {
                "launched": True,
                "port": port,
                "mode": mode,
                "boot_seconds": round(time.time() - t0, 1),
                "pid": self.ansa_pid,
                "log": str(LOG_DIR / f"ansa_{port}.log"),
            }

    def attach(self, port: int, wait: float = 60.0) -> dict:
        with self._lock:
            self._close_client()
            self.client = connect_with_retry(port, timeout=DEFAULT_TIMEOUT,
                                            deadline=wait, quiet=True)
            self.port = port
            self.mode = "external"
            self.proc = None
            self.started_at = time.time()
            self.ansa_pid = self._probe_pid()
            self._save_state()
            return {"attached": True, "port": port, "pid": self.ansa_pid}

    def _alive(self) -> bool:
        if self.client is None:
            return False
        try:
            res = self.client.execute("def main():\n    return {'p': '1'}\n",
                                      "main", timeout=20.0)
            return res.ok
        except (IapError, OSError):
            return False

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    @staticmethod
    def _tasklist(pid: int) -> str:
        """Decoded ``tasklist`` row for one pid, or "" when it is not running.

        ``tasklist`` writes in the OEM/ANSI code page on a Chinese Windows, so
        the bytes are decoded as ``mbcs`` rather than assumed UTF-8. Reading it
        as text would otherwise raise and leave the caller with no data.
        """
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        raw = proc.stdout or b""
        try:
            return raw.decode("mbcs", "replace")
        except LookupError:
            return raw.decode("utf-8", "replace")

    @classmethod
    def _pid_alive(cls, pid: int) -> bool:
        row = cls._tasklist(pid)
        return bool(row.strip()) and "no tasks" not in row.lower() \
            and str(pid) in row

    @classmethod
    def _pid_is_ansa(cls, pid: int) -> bool:
        """Confirm a pid still belongs to ANSA before we kill anything.

        After a reboot a recycled pid can point at an unrelated process, so the
        state file alone is not enough evidence to justify a ``taskkill``.
        """
        row = cls._tasklist(pid)
        if not row.strip() or str(pid) not in row:
            return False
        low = row.lower()
        return "ansa" in low or "beta_cae" in low

    def _wait_pid_exit(self, pid: int, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self._pid_alive(pid):
                return True
            time.sleep(0.25)
        return not self._pid_alive(pid)

    def _probe_pid(self) -> int | None:
        """Ask ANSA for its own pid; unlike ``proc.pid`` this is the real one."""
        payload = self._raw_execute("import os\nos.getpid()\n", timeout=20.0)
        if payload is None:
            return None
        try:
            value = json.loads(payload.get("result") or "null")
        except ValueError:
            return None
        return value if isinstance(value, int) else None

    def _kill_orphans(self) -> None:
        """Retire an ANSA process this server launched in an earlier run.

        Scoped strictly to the recorded ANSA pid *and* re-verified as ANSA. Any
        other ANSA window the user opened by hand, and any pid that has since
        been recycled, is left alone.
        """
        state = self._load_state()
        ansa_pid = state.get("ansa_pid")
        if isinstance(ansa_pid, int) and self._pid_is_ansa(ansa_pid):
            _log(f"terminating previously launched ANSA pid {ansa_pid}")
            subprocess.run(["taskkill", "/F", "/PID", str(ansa_pid)],
                           capture_output=True, check=False)
        elif ansa_pid:
            _log(f"orphan pid {ansa_pid} is no longer ANSA; leaving it alone")
        self._close_client()
        self.proc = None
        self.ansa_pid = None

    def ensure(self, mode: str = "gui", wait: float = 240.0,
               allow_launch: bool = True) -> None:
        """Make sure a usable connection exists, launching ANSA if needed.

        ``allow_launch=False`` only reconnects to a session that already
        exists. That is the right mode for a retry: spawning a fresh ANSA
        would silently run the script against an empty database, which looks
        like success but has nothing to do with the model being edited.
        """
        with self._lock:
            if self.client is not None and self._alive():
                return
            self._close_client()
            for port in (self._load_state().get("port"), self.port):
                if not port:
                    continue
                try:
                    self.client = connect_with_retry(port, deadline=15.0, quiet=True)
                    self.port = port
                    self.mode = self.mode or "external"
                    _log(f"reattached to ANSA on port {port}")
                    return
                except IapError:
                    continue
            if not allow_launch:
                raise IapError(
                    "ANSA is not reachable and auto-launch is disabled for "
                    "this retry; nothing was re-executed"
                )
            self.launch(mode=mode, wait=wait)

    # -- execution ---------------------------------------------------------
    def _raw_execute(self, code: str, timeout: float) -> dict | None:
        """Run a tiny internal script without touching ledger or step log."""
        try:
            with self._lock:
                if self.client is None:
                    return None
                res = self.client.execute(_build_script(code), "main", timeout=timeout)
        except (IapError, OSError):
            return None
        if not res.ok or res.payload.get("ok") != "1":
            return None
        return res.payload

    def _counts(self) -> dict[str, int] | None:
        """Cheap entity census used as before/after evidence for a change."""
        listing = ", ".join(repr(t) for t in COUNT_TYPES)
        code = (
            "_deck = base.CurrentDeck()\n"
            "_c = {}\n"
            f"for _t in ({listing}):\n"
            "    try:\n"
            "        _c[_t] = len(base.CollectEntities(_deck, None, _t))\n"
            "    except Exception:\n"
            "        _c[_t] = None\n"
            "_c\n"
        )
        payload = self._raw_execute(code, timeout=60.0)
        if payload is None:
            return None
        try:
            parsed = json.loads(payload.get("result") or "null")
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _record_step(tool: str, op_id: str, status: str,
                     delta: dict | None, mutating: bool, note: str = "",
                     record: bool = True) -> None:
        if not record:
            return
        STEPS.append({
            "seq": next(_STEP_SEQ),
            "tool": tool,
            "operation_id": op_id,
            "status": status,
            "mutating": bool(mutating),
            "delta": delta or {},
            "at": time.strftime("%H:%M:%S"),
            "note": note,
        })

    def run(self, code: str, timeout: float = DEFAULT_TIMEOUT, *,
            tool: str = "ansa_run_python", params: dict | None = None,
            operation_id: str = "", mutating: bool = False,
            measure: bool | None = None, record: bool = True) -> dict:
        """Execute one script, with idempotency and change evidence.

        ``mutating`` switches on the ledger: the claim is recorded on disk
        before the script is sent, so a lost response can be answered later
        instead of being re-applied.
        """
        params = dict(params or {})
        op_id = (operation_id or "").strip() or new_operation_id()
        fp = fingerprint(tool, params) if mutating else ""
        measure = bool(mutating) if measure is None else bool(measure)

        if mutating:
            try:
                cached = LEDGER.reserve(op_id, tool, fp)
            except OperationOutcomeUnknown as exc:
                self._record_step(tool, op_id, "OUTCOME_UNKNOWN", None, mutating,
                                  note="refused by ledger", record=record)
                return {"ok": False, "status": "OUTCOME_UNKNOWN", "error": str(exc),
                        "operation_id": op_id, "phase": "ledger",
                        "mutating": mutating}
            except LedgerError as exc:
                self._record_step(tool, op_id, "REFUSED", None, mutating,
                                  note="refused by ledger", record=record)
                return {"ok": False, "status": "REFUSED", "error": str(exc),
                        "operation_id": op_id, "phase": "ledger",
                        "mutating": mutating}
            if cached is not None:
                return {"ok": True, "status": "REPLAYED", "operation_id": op_id,
                        "elapsed": cached.get("elapsed", "?"),
                        "result": cached.get("summary", ""),
                        "delta": (cached.get("extra") or {}).get("delta", {}),
                        "mutating": mutating, "measured": measure,
                        "replayed": True,
                        "note": "identical operation already succeeded; the "
                                "stored result was replayed and ANSA was not "
                                "called again"}

        script = _build_script(code)
        before = self._counts() if measure else None

        try:
            with self._lock:
                if self.client is None:
                    self.ensure()
                res = self.client.execute(script, "main", timeout=timeout)
        except IapOutcomeUnknown as exc:
            return self._unknown(exc, op_id, tool, mutating, record)
        except (IapError, OSError) as exc:
            # The message never made it across a dead socket, so replaying it
            # on a fresh connection cannot double-apply anything. Reconnect
            # only - launching a new ANSA would run the script on a blank model.
            self.last_error = str(exc)
            self._close_client()
            try:
                self.ensure(allow_launch=False)
                before = self._counts() if measure else None
                res = self.client.execute(script, "main", timeout=timeout)
            except IapOutcomeUnknown as exc2:
                return self._unknown(exc2, op_id, tool, mutating, record)
            except (IapError, OSError) as exc2:
                if mutating:
                    LEDGER.finish(op_id, FAILED, error=str(exc2))
                self._record_step(tool, op_id, "TRANSPORT_FAILED", None, mutating,
                                  record=record)
                return {"ok": False, "status": "TRANSPORT_FAILED",
                        "error": str(exc2), "operation_id": op_id,
                        "phase": "transport", "retryable": True,
                        "mutating": mutating}

        after = self._counts() if measure else None
        delta = _delta(before, after)
        payload = res.payload
        ok = bool(res.ok and payload.get("ok") == "1")

        out: dict[str, Any] = {
            "ok": ok,
            "operation_id": op_id,
            "status": "OK" if ok else "SCRIPT_ERROR",
            "stdout": payload.get("stdout", ""),
            "result": payload.get("result", ""),
            "elapsed": payload.get("elapsed", f"{res.elapsed:.2f}"),
            "import_error": payload.get("import_error", ""),
            "delta": delta,
            "mutating": mutating,
            "measured": measure,
        }
        if measure:
            out["counts_before"] = before
            out["counts_after"] = after
        if not ok:
            out["error"] = payload.get("error") or res.describe()
            out["iap"] = res.describe()

        if mutating:
            if ok:
                summary = (payload.get("result") or payload.get("stdout") or "")[:4000]
                LEDGER.finish(op_id, SUCCESS, summary=summary,
                              elapsed=res.elapsed, extra={"delta": delta})
            else:
                # A raised script is a clean failure only when ANSA itself
                # reported it; the transport delivered a real answer.
                LEDGER.finish(op_id, FAILED, error=out.get("error", ""),
                              elapsed=res.elapsed, extra={"delta": delta})

        self._record_step(tool, op_id, out["status"], delta, mutating,
                          record=record)
        return out

    def _unknown(self, exc: Exception, op_id: str, tool: str,
                 mutating: bool, record: bool = True) -> dict:
        if mutating:
            LEDGER.finish(op_id, UNKNOWN, error=str(exc))
        self._close_client()
        self._record_step(tool, op_id, "OUTCOME_UNKNOWN", None, mutating,
                          record=record)
        return {"ok": False, "status": "OUTCOME_UNKNOWN", "error": str(exc),
                "operation_id": op_id, "phase": "execute",
                "retryable": False, "mutating": mutating}


    def shutdown(self, force: bool = False, allow_external: bool = False) -> dict:
        """Stop the ANSA instance this server launched.

        An attached session - one the user started by hand - is only
        disconnected, never quit or killed: it may hold unsaved work, and an
        agent should not be able to destroy it by accident.
        """
        with self._lock:
            if self.client is None and self.proc is None:
                return {"shutdown": False,
                        "reason": "no ANSA process owned by this server"}

            if self.proc is None and not allow_external:
                self._close_client()
                self.port = None
                self.mode = None
                self.clear_state()
                return {
                    "shutdown": False,
                    "disconnected": True,
                    "reason": "this is an externally started ANSA session; the "
                              "server disconnected but left it running. Pass "
                              "allow_external=true to quit it as well.",
                }

            info: dict[str, Any] = {}
            try:
                if self.client is not None:
                    self.client.execute("def main():\n    session.Quit(0)\n    return {'q': '1'}\n",
                                        "main", timeout=8.0)
                    info["quit_sent"] = True
            except (IapError, OSError):
                pass
            self._close_client()

            # Prefer ANSA's real pid; fall back to the launcher wrapper's tree.
            grace = 3.0 if force else 8.0
            exited = False
            if self.ansa_pid:
                exited = self._wait_pid_exit(self.ansa_pid, grace)
            elif self.proc is not None:
                try:
                    self.proc.wait(timeout=grace)
                    exited = True
                except subprocess.TimeoutExpired:
                    exited = self.proc.poll() is not None
            info["exited_gracefully"] = exited

            killed: list[int] = []
            if not exited:
                # Only ever touch pids that are still identifiable as ANSA, or
                # the launcher this server itself created.
                if self.ansa_pid and self._pid_is_ansa(self.ansa_pid):
                    subprocess.run(["taskkill", "/F", "/PID", str(self.ansa_pid)],
                                   capture_output=True, check=False)
                    killed.append(self.ansa_pid)
                if self.proc is not None and self.proc.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True, check=False)
                    killed.append(self.proc.pid)
            if killed:
                info["taskkill"] = killed
            self.proc = None
            self.ansa_pid = None
            self.port = None
            self.mode = None
            self.clear_state()
            info["shutdown"] = True
            return info

    def status(self) -> dict:
        alive = self.client is not None and self._alive()
        return {
            "connected": alive,
            "port": self.port,
            "mode": self.mode,
            "pid": self.ansa_pid,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else None,
            "last_error": self.last_error,
            "workspace": WORKSPACE or None,
            "ledger_records": LEDGER.size(),
            "unresolved_operations": LEDGER.pending_count(),
        }


SESSION = AnsaSession()


# ------------------------------------------------------------ result format ---
def _evidence(res: dict) -> str:
    """One line that keeps three kinds of evidence apart.

    "The API did not raise" says nothing about whether the model is right, so
    the transport result, the script result and the observed model change are
    always reported separately.
    """
    status = res.get("status", "")
    if status == "OUTCOME_UNKNOWN":
        transport, script = "sent, no reply", "unknown"
    elif status == "TRANSPORT_FAILED":
        transport, script = "failed before send", "not executed"
    elif status in ("REFUSED", "REPLAYED"):
        transport, script = "not used", "not executed"
    elif res.get("ok"):
        transport, script = "ok", "returned"
    else:
        transport, script = "ok", "raised"

    if res.get("mutating"):
        if res.get("measured") is False:
            model = "not measured for this call"
        else:
            model = _fmt_delta(res.get("delta") or {})
    else:
        model = "not tracked (call declared read-only)"

    return (f"evidence: transport={transport} · script={script} · "
            f"model={model} · engineering=unverified")


def _fmt(res: dict) -> str:
    status = res.get("status", "")
    op = res.get("operation_id")

    if status == "OUTCOME_UNKNOWN":
        return "\n".join([
            "**OUTCOME UNKNOWN - the model may already have been changed**",
            "",
            f"ANSA accepted the request but never returned a result: "
            f"{res.get('error', '')}",
            "",
            f"- operation_id: `{op}`",
            "- Do **not** resend this call with a fresh operation_id.",
            "- Read the model back first (`ansa_model_info`). If nothing changed,",
            f"  retry with the same id `{op}` - the ledger will let it run once,",
            "  or replay the recorded result if it did in fact succeed.",
            "",
            _evidence(res),
        ])

    if status == "REFUSED":
        return ("**operation refused - nothing was sent to ANSA**\n\n"
                + (res.get("error") or "")
                + (f"\n\noperation_id: `{op}`" if op else "")
                + "\n\n" + _evidence(res))

    if status == "TRANSPORT_FAILED":
        return ("**could not reach ANSA - nothing was executed**\n\n"
                + (res.get("error") or "")
                + (f"\n\noperation_id: `{op}`" if op else "")
                + "\n\nThe call is retryable as-is.\n\n" + _evidence(res))

    if status == "REPLAYED":
        parts = [f"ok · replayed from ledger · op `{op}`", str(res.get("note", ""))]
        body = res.get("result") or ""
        if body.strip():
            parts.append("stored result:\n```\n" + body.rstrip() + "\n```")
        parts.append(_evidence(res))
        return "\n".join(parts)

    parts: list[str] = []
    if not res.get("ok"):
        parts.append("**ANSA script raised an error**")
        if (res.get("stdout") or "").strip():
            parts.append("output before the failure:\n```\n" + res["stdout"].rstrip() + "\n```")
        parts.append("```\n" + (res.get("error") or "unknown error").strip() + "\n```")
        iap = res.get("iap")
        if iap and iap != "result=success":
            parts.append(f"transport: `{iap}`")
    else:
        head = f"ok · {res.get('elapsed', '?')}s"
        if op:
            head += f" · op `{op}`"
        parts.append(head)
        if (res.get("stdout") or "").strip():
            parts.append("```\n" + res["stdout"].rstrip() + "\n```")
        result = res.get("result") or ""
        if result:
            if len(result) > 200 or "\n" in result:
                parts.append("result:\n```json\n" + result + "\n```")
            else:
                parts.append(f"result: `{result}`")
        if res.get("import_error"):
            parts.append("note - some ansa modules failed to import:\n```\n"
                         + res["import_error"].strip() + "\n```")
    parts.append(_evidence(res))
    return "\n".join(parts)


def _resolve_out(value: str) -> tuple[str | None, str]:
    """Validate a path this server is about to write.

    Returns ``(posix_path, error)``. When ``ANSA_MCP_WORKSPACE`` is set the
    path must resolve inside it, which keeps exports from silently landing
    somewhere unrelated. Relative paths are resolved against the workspace
    (or the process directory when no workspace is configured).
    """
    if not value or not str(value).strip():
        return None, "path is empty"
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute():
        raw = (Path(WORKSPACE) if WORKSPACE else Path.cwd()) / raw
    target = Path(os.path.normpath(str(raw)))

    if WORKSPACE:
        root = os.path.normcase(os.path.abspath(WORKSPACE))
        here = os.path.normcase(os.path.abspath(str(target)))
        try:
            inside = os.path.commonpath([root, here]) == root
        except ValueError:
            inside = False
        if not inside:
            return None, (
                f"`{target}` is outside ANSA_MCP_WORKSPACE ({WORKSPACE}). "
                "Use a relative path inside the workspace, or unset "
                "ANSA_MCP_WORKSPACE to allow writing anywhere."
            )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"cannot create parent directory: {exc}"
    return str(target).replace("\\", "/"), ""


def _strict_bool(value: Any, name: str) -> tuple[bool | None, str]:
    """Reject truthy impostors such as ``1`` or ``"true"``.

    The FastMCP layer already coerces declared ``bool`` params, but an agent
    that passes a string through a generic dispatch path would otherwise get
    an accidental ``True``. Explicit is safer for destructive switches.
    """
    if isinstance(value, bool):
        return value, ""
    if value is None:
        return False, ""
    return None, f"`{name}` must be true or false, got {type(value).__name__} {value!r}"


def _delta(before: dict | None, after: dict | None) -> dict[str, int]:
    """Per-type entity count difference, skipping types we could not read."""
    if not before or not after:
        return {}
    out: dict[str, int] = {}
    for key, old in before.items():
        new = after.get(key)
        if isinstance(old, int) and isinstance(new, int) and old != new:
            out[key] = new - old
    return out


def _fmt_delta(delta: dict[str, int]) -> str:
    if not delta:
        return "unchanged"
    return ", ".join(f"{k} {v:+d}" for k, v in sorted(delta.items()))



# ------------------------------------------------------------------- tools ---
@mcp.tool()
def ansa_status() -> str:
    """Report whether an ANSA process is reachable and what model is loaded.

    Safe to call at any time; does not launch ANSA.
    """
    st = SESSION.status()
    lines = [
        f"- connected: **{st['connected']}**",
        f"- port: {st['port']}",
        f"- mode: {st['mode']}",
        f"- pid: {st['pid']}",
        f"- uptime: {st['uptime_s']} s",
        f"- workspace: {st['workspace'] or '(unrestricted)'}",
        f"- ledger: {st['ledger_records']} record(s), "
        f"{st['unresolved_operations']} unresolved",
    ]
    if st["last_error"]:
        lines.append(f"- last error: `{st['last_error']}`")
    if st["unresolved_operations"]:
        lines.append(
            "- ⚠ unresolved operations exist: a previous write was sent but "
            "never confirmed. Check the model before making more changes; see "
            "`ansa_step_history`."
        )
    if st["connected"]:
        probe = SESSION.run(
            "import json\n"
            "info = session.ApplicationInformation()\n"
            "deck = base.CurrentDeck()\n"
            "counts = {}\n"
            "for t in ('GRID', 'SHELL', 'SOLID', 'BEAM', 'PART', 'GROUP', " 
            "'__PROPERTIES__', '__MATERIALS__'):\n"
            "    try:\n"
            "        counts[t] = len(base.CollectEntities(deck, None, t))\n"
            "    except Exception:\n"
            "        counts[t] = -1\n"
            "ver = [l.strip() for l in info.splitlines() if 'version' in l]\n"
            "{'version': ver[0] if ver else '?', 'deck': deck, 'counts': counts}\n",
            tool="ansa_status", record=False,
        )
        if probe["ok"]:
            lines.append("- ANSA: " + (probe["result"] or probe["stdout"]))
        else:
            lines.append("- probe failed: " + (probe["error"] or "")[:400])
    else:
        lines.append("\nCall `ansa_launch` to start ANSA (gui mode by default).")
    return "\n".join(lines)


@mcp.tool()
def ansa_launch(mode: str = "gui", port: int = 0, model: str = "",
                open_format: str = "auto", extra_args: str = "",
                wait_seconds: int = 240) -> str:
    """Start a fresh ANSA process and connect to it.

    Args:
        mode: "gui" (interactive window, default) or "batch" (no window, faster boot).
        port: IAP listen port; 0 picks a free port automatically.
        model: optional file to import right after boot (see `ansa_open` for
              the supported formats).
        open_format: how to interpret `model`; "auto" picks by extension.
        extra_args: extra ANSA command line switches, space separated.
        wait_seconds: how long to wait for the IAP handshake.
    """
    args = extra_args.split() if extra_args.strip() else None
    try:
        info = SESSION.launch(mode=mode, port=port or None,
                              extra_args=args, wait=float(wait_seconds))
    except Exception as exc:  # noqa: BLE001
        return f"**launch failed**: {type(exc).__name__}: {exc}"
    if info.get("already_running"):
        head = f"ANSA already running on port {info['port']} ({info['mode']})."
    else:
        head = (f"ANSA {mode} started in {info['boot_seconds']}s\n"
                f"- port: {info['port']}\n- pid: {info['pid']}\n- log: `{info['log']}`")
    if model:
        head += f"\n\n**importing {model}**\n" + ansa_open(model, open_format)
    return head


@mcp.tool()
def ansa_attach(port: int, wait_seconds: int = 60) -> str:
    """Connect to an ANSA instance you started yourself.

    Start it manually with: ``ansa64.bat -nolauncher -listenport <port> -foregr``
    """
    try:
        SESSION.attach(port, wait=float(wait_seconds))
    except Exception as exc:  # noqa: BLE001
        return f"**attach failed**: {type(exc).__name__}: {exc}"
    return f"attached to ANSA on port {port}"


@mcp.tool()
def ansa_shutdown(force: bool = False, allow_external: bool = False) -> str:
    """Quit the ANSA process owned by this server.

    An instance this server launched is quit via `session.Quit(0)` and, if it
    hangs, killed by its own pid. An instance you started by hand is only
    disconnected - it may hold unsaved work, so it is never quit or killed
    unless you explicitly pass `allow_external=true`.

    Args:
        force: shorten the graceful-exit grace period from 8s to 3s.
        allow_external: also quit an externally started session this server
              is merely attached to.
    """
    f1, err1 = _strict_bool(force, "force")
    f2, err2 = _strict_bool(allow_external, "allow_external")
    if f1 is None or f2 is None:
        return f"**{err1 or err2}**"
    info = SESSION.shutdown(force=f1, allow_external=f2)
    return json.dumps(info, indent=2)


@mcp.tool()
def ansa_run_python(code: str, timeout: int = 600, mutating: bool = False,
                    operation_id: str = "") -> str:
    """Execute python code inside the running ANSA process.

    This is the main workhorse - anything the ANSA python API can do, you can
    do here. Pre-imported names: ``ansa, base, constants, session, mesh,
    batchmesh, utils, morph, guitk``. ``print()`` output is captured and
    returned. ``ansavars`` is a dict that persists between calls.
    The last expression in the snippet becomes the returned ``result``.

    Args:
        code: python statements. Can be multi-line.
        timeout: seconds to wait before giving up (default 600).
        mutating: set true when the snippet changes the model (creates,
              deletes, transforms, meshes, assigns). This turns on before/after
              entity counts and makes the call idempotent, so a lost response
              cannot be applied twice. Leave false for pure reads - read-only
              calls are safely auto-retried.
        operation_id: reuse a previous id to retry the same operation safely.
              Empty means the server generates one and returns it.
    """
    flag, err = _strict_bool(mutating, "mutating")
    if flag is None:
        return f"**{err}**"
    res = SESSION.run(code, timeout=float(timeout), tool="ansa_run_python",
                      params={"code": code}, operation_id=operation_id,
                      mutating=flag)
    return _fmt(res)


@mcp.tool()
def ansa_open(path: str, file_format: str = "auto", merge: bool = True,
              confirm: bool = False, timeout: int = 1800,
              operation_id: str = "") -> str:
    """Import a file into ANSA - CAD geometry, solver input, or an .ansa session.

    The right importer is picked from the extension:
      .step/.stp .igs/.iges .jt .x_t .sldprt ... -> CAD translator (base.Open)
      .nas/.bdf/.dat  -> Nastran input
      .inp/.inc       -> Abaqus input
      .key/.k         -> LS-DYNA input
      .cdb            -> Ansys input
      .ansa           -> native ANSA session
    Anything unrecognised falls back to the CAD translator.

    Args:
        path: absolute path to the file.
        file_format: "auto" (default), "cad", or an explicit solver name such
              as nastran / abaqus / ansys / lsdyna / pamcrash / radioss /
              patran / stl. Use an explicit value when the extension is
              ambiguous, e.g. a Nastran .dat that is not a PERMAS file.
        merge: True (default) appends into the current model, False replaces
              it and discards whatever is loaded.
        confirm: required when merge=false, because replacing the model
              destroys unsaved work.
        timeout: imports of large CAD files can take many minutes.
        operation_id: reuse to retry the same import safely.
    """
    src = Path(path).expanduser()
    if not src.is_absolute():
        src = Path.cwd() / src
    p = str(src.resolve()).replace("\\", "/")
    if not src.exists():
        return f"**file not found**: {p}"

    merge_flag, err = _strict_bool(merge, "merge")
    if merge_flag is None:
        return f"**{err}**"
    ok_flag, _ = _strict_bool(confirm, "confirm")
    if ok_flag is None:
        return "**`confirm` must be true or false**"
    if not merge_flag and not ok_flag:
        return ("**refusing to replace the current model**\n\n"
                "`merge=false` discards everything currently loaded and "
                "cannot be undone. Save first (`ansa_save`), then call again "
                "with `confirm=true`.")

    ext = Path(p).suffix.lower()
    try:
        func, fmt = _route_input(ext, file_format)
    except ValueError as exc:
        return f"**{exc}**"

    if func is None:
        call = f"base.Open({p!r})"
    else:
        action = "merge_model" if merge_flag else "overwrite_model"
        call = f"base.{func}({p!r}, model_action={action!r})"

    code = (
        f"ret = {call}\n"
        "counts = {}\n"
        "deck = base.CurrentDeck()\n"
        "for t in ('GRID', 'SHELL', 'SOLID', 'BEAM', 'PART', 'GROUP',\n"
        "          'PSHELL', 'PSOLID', 'MAT1', '_PROPERTIES_'):\n"
        "    try:\n"
        "        counts[t] = len(base.CollectEntities(deck, None, t))\n"
        "    except Exception:\n"
        "        counts[t] = None\n"
        f"{{'importer': {'base.Open' if func is None else 'base.' + func!r}, "
        f"'format': {fmt!r}, 'return': ret, 'deck': deck,\n"
        " 'counts': {k: v for k, v in counts.items() if v}}\n"
    )
    res = SESSION.run(code, timeout=float(timeout), tool="ansa_open",
                      params={"path": p, "file_format": fmt, "merge": merge_flag},
                      operation_id=operation_id, mutating=True)
    return _fmt(res)


@mcp.tool()
def ansa_save(path: str = "", overwrite: bool = False, timeout: int = 600,
              operation_id: str = "") -> str:
    """Save the ANSA database.

    Args:
        path: destination .ansa file. Empty means "save in place", which
              overwrites the current file by design.
        overwrite: required when `path` already exists, so an accidental
              second run cannot silently replace an older snapshot.
        timeout: seconds to wait.
        operation_id: reuse to retry the same save safely.
    """
    flag, err = _strict_bool(overwrite, "overwrite")
    if flag is None:
        return f"**{err}**"

    if not path:
        res = SESSION.run("base.Save()\n{'saved': 'in place'}\n",
                          timeout=float(timeout), tool="ansa_save",
                          params={"path": "", "overwrite": True},
                          operation_id=operation_id, mutating=True)
        return _fmt(res)

    p, err = _resolve_out(path)
    if p is None:
        return f"**{err}**"
    if Path(p).exists() and not flag:
        return (f"**{p} already exists**\n\n"
                "Pass `overwrite=true` to replace it, or choose a new path. "
                "Refusing by default so an earlier snapshot is not lost.")

    res = SESSION.run(f"base.SaveAs({p!r})\n{{'saved': {p!r}}}\n",
                      timeout=float(timeout), tool="ansa_save",
                      params={"path": p, "overwrite": flag},
                      operation_id=operation_id, mutating=True)
    return _fmt(res)


@mcp.tool()
def ansa_export(path: str, deck: str = "nastran", options: str = "",
                overwrite: bool = False, timeout: int = 1800,
                operation_id: str = "") -> str:
    """Write a solver input file (FILE>OUTPUT>...).

    Args:
        path: output file path; the extension is up to you.
        deck: nastran | abaqus | ansys | lsdyna | optistruct | pamcrash |
              radioss | marc | ideas | patran | stl.
        options: extra keyword arguments for the output call, e.g.
              ``{"disregard_includes": "on"}`` (must be a JSON object).
        overwrite: required when the target already exists. Refusing by
              default makes a stale file from an earlier run impossible to
              mistake for a fresh one.
        timeout: output can take a while on big models.
        operation_id: reuse to retry the same export safely.
    """
    fn = DECK_OUTPUT.get(deck.lower())
    if not fn:
        return (f"unknown deck `{deck}`. Supported: " + ", ".join(sorted(DECK_OUTPUT)))

    flag, err = _strict_bool(overwrite, "overwrite")
    if flag is None:
        return f"**{err}**"

    p, err = _resolve_out(path)
    if p is None:
        return f"**{err}**"
    if Path(p).exists() and not flag:
        return (f"**{p} already exists**\n\n"
                "Pass `overwrite=true` to replace it, or pick a new filename. "
                "Refusing by default avoids mixing results from two runs.")

    kw: dict[str, Any] = {"filename": p}
    if options.strip():
        try:
            kw.update(json.loads(options))
        except ValueError as exc:
            return f"**options must be a JSON object**: {exc}"
    kwargs = "".join(f", {k}={v!r}" for k, v in kw.items())
    code = f"ret = base.{fn}({kwargs[2:]})\n{{'output': {fn!r}, 'file': {p!r}, 'return': ret}}\n"
    res = SESSION.run(code, timeout=float(timeout), tool="ansa_export",
                      params={"path": p, "deck": deck, "options": options},
                      operation_id=operation_id, mutating=True, measure=False)
    return _fmt(res)


@mcp.tool()
def ansa_model_info(detail: bool = False) -> str:
    """Summarise the loaded model: entity counts per type, parts, properties.

    Args:
        detail: also list part names, property ids and materials.
    """
    code = """
deck = base.CurrentDeck()
types = ['GRID', 'SHELL', 'SOLID', 'BEAM', 'SPRING', 'MASS', 'RIGID',
         'PART', 'GROUP', 'INCLUDE', 'SET', '__PROPERTIES__', '__MATERIALS__']
counts = {}
for t in types:
    try:
        counts[t] = len(base.CollectEntities(deck, None, t))
    except Exception:
        pass
out = {'deck': deck, 'counts': counts}
if __ANSA_MCP_DETAIL__:
    def aggregate(collection, named_limit=40):
        by_type, named = {}, []
        for e in collection:
            try:
                t = e.ansa_type(deck)
            except Exception:
                t = '?'
            by_type[t] = by_type.get(t, 0) + 1
            try:
                nm = e._name
            except Exception:
                nm = ''
            if nm and 'Default' not in nm and len(named) < named_limit:
                named.append({'id': e._id, 'type': t, 'name': nm})
        return by_type, named

    for t, key in (('PART', 'parts'), ('__PROPERTIES__', 'properties'),
                   ('__MATERIALS__', 'materials'), ('GROUP', 'groups'),
                   ('SET', 'sets')):
        try:
            ents = base.CollectEntities(deck, None, t)
        except Exception:
            continue
        if not ents:
            continue
        by_type, named = aggregate(ents)
        out[key + '_by_type'] = by_type
        if named:
            out[key + '_named'] = named
out
""".replace("__ANSA_MCP_DETAIL__", "True" if detail else "False")
    return _fmt(SESSION.run(code, tool="ansa_model_info"))


@mcp.tool()
def ansa_list_entities(entity_type: str, limit: int = 100, fields: str = "",
                       timeout: int = 300) -> str:
    """List entities of one type with their ids (and optionally card values).

    Args:
        entity_type: ANSA entity type, e.g. NODE, SHELL, SOLID, PART, GROUP,
              PSHELL, MAT1. Deck-specific names apply (check with
              `ansa_api_search` or `ansa_model_info`).
        limit: max number of entities returned (keeps the answer readable).
        fields: comma separated card labels to read, e.g. "Name,Thickness".
    """
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    code = f"""
deck = base.CurrentDeck()
ents = base.CollectEntities(deck, None, {entity_type!r})
total = len(ents)
wanted = {field_list!r}
rows = []
for e in ents[:{int(limit)}]:
    row = {{'id': e._id}}
    try:
        t = e.ansa_type(deck)
        if t != {entity_type!r}:
            row['type'] = t
    except Exception:
        pass
    try:
        if e._name:
            row['name'] = e._name
    except Exception:
        pass
    if wanted:
        try:
            vals = base.GetEntityCardValues(deck, e, wanted)
            row.update({{str(k): str(v) for k, v in vals.items()}})
        except Exception as exc:
            row['card_error'] = str(exc)
    rows.append(row)
{{'type': {entity_type!r}, 'total': total, 'returned': len(rows), 'entities': rows}}
"""
    return _fmt(SESSION.run(code, timeout=float(timeout), tool="ansa_list_entities"))


@mcp.tool()
def ansa_get_entity_values(entity_type: str, entity_id: int, fields: str = "",
                           timeout: int = 120) -> str:
    """Read the edit-card values of a single entity.

    Args:
        entity_type: e.g. PSHELL, MAT1, SHELL, NODE.
        entity_id: entity id.
        fields: comma separated card labels; empty means "all fields".
    """
    field_list = [f.strip() for f in fields.split(",") if f.strip()]
    code = f"""
deck = base.CurrentDeck()
ent = base.GetEntity(deck, {entity_type!r}, {int(entity_id)})
if ent is None:
    raise ValueError('entity {entity_type} id {int(entity_id)} not found')
all_fields = list(ent.card_fields(deck))
wanted = {field_list!r} or all_fields
vals = base.GetEntityCardValues(deck, ent, wanted)
{{'type': {entity_type!r}, 'id': {int(entity_id)},
  'available_fields': all_fields if {bool(field_list)!r} else 'all',
  'values': {{str(k): str(v) for k, v in vals.items() if str(v) not in ('', 'None')}}}}
"""
    return _fmt(SESSION.run(code, timeout=float(timeout), tool="ansa_get_entity_values"))


@mcp.tool()
def ansa_mesh(entities: str = "PSHELL", algorithm: str = "batch",
              timeout: int = 1800, operation_id: str = "") -> str:
    """Generate mesh on properties/parts using an ANSA meshing algorithm.

    Args:
        entities: entity type whose owners get meshed, usually PSHELL
              (2D shells) or PSOLID (3D). Use PART to mesh whole parts.
        algorithm: "batch" uses ANSA's batch generator (recommended, honours
              the BatchMesh parameters), "free" / "best" / "map" call the
              classic visible-macro algorithms.
        timeout: meshing can take a long time.
        operation_id: reuse to retry the same meshing run safely. Meshing is
              the operation most likely to outlive a client timeout, so this
              matters: a blind retry would mesh the model twice.
    """
    algo = algorithm.lower()
    if algo == "batch":
        code = f"""
deck = base.CurrentDeck()
ents = base.CollectEntities(deck, None, {entities!r})
if not ents:
    raise ValueError('no {entities} found to mesh')
before = len(base.CollectEntities(deck, None, 'SHELL')) + len(base.CollectEntities(deck, None, 'SOLID'))
mesh.BatchGenerator(ents)
after = len(base.CollectEntities(deck, None, 'SHELL')) + len(base.CollectEntities(deck, None, 'SOLID'))
{{'algorithm': 'BatchGenerator', 'meshed_entities': len(ents),
  'elements_before': before, 'elements_after': after}}
"""
    else:
        fn = {"free": "CreateFreeMesh", "best": "CreateBestMesh",
              "map": "CreateMapMesh"}.get(algo)
        if not fn:
            return 'algorithm must be one of: batch, free, best, map'
        code = f"""
deck = base.CurrentDeck()
before = len(base.CollectEntities(deck, None, 'SHELL')) + len(base.CollectEntities(deck, None, 'SOLID'))
ret = mesh.{fn}()
after = len(base.CollectEntities(deck, None, 'SHELL')) + len(base.CollectEntities(deck, None, 'SOLID'))
{{'algorithm': {fn!r}, 'return': ret, 'elements_before': before, 'elements_after': after}}
"""
    res = SESSION.run(code, timeout=float(timeout), tool="ansa_mesh",
                      params={"entities": entities, "algorithm": algo},
                      operation_id=operation_id, mutating=True)
    return _fmt(res)


# The whole-window grab runs inside ANSA. SnapShotWindow() wants an internal
# BC window title, which the MDI main window does not have, and SnapShot()
# only ever captures the graphics viewport. PrintWindow with
# PW_RENDERFULLCONTENT (2) does capture the menu bar, toolbars, model tree,
# side panels and the console - verified on ANSA 23.1.1 / Windows 10 19045,
# 3697x1346 px, 98.6% non-black pixels.
_WINDOW_SHOT_CODE = r'''
import ctypes, os, struct, zlib
from ctypes import wintypes
import numpy as np

def _mcp_list_windows():
    u = ctypes.windll.user32
    own = os.getpid()
    hits = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != own:
            return True
        rect = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        hits.append({"title": buf.value, "hwnd": int(hwnd),
                     "width": rect.right - rect.left,
                     "height": rect.bottom - rect.top})
        return True

    u.EnumWindows(proc(_cb), 0)
    return hits


class _MCPBITMAPINFO(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _mcp_grab_window(hwnd):
    """PrintWindow the given hwnd and return (width, height, rgb_bytes)."""
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise ValueError("window has no drawable size")
    hdc = u.GetWindowDC(hwnd)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    old = g.SelectObject(mem, bmp)
    try:
        ok = u.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT
        if not ok:
            u.PrintWindow(hwnd, mem, 0)
        bi = _MCPBITMAPINFO()
        bi.biSize = ctypes.sizeof(_MCPBITMAPINFO)
        bi.biWidth = w
        bi.biHeight = -h          # negative => top-down rows
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        raw = buf.raw
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(hwnd, hdc)
    bgra = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
    return w, h, bgra[:, :, [2, 1, 0]]


def _mcp_write_png(path, rgb):
    h, w, _ = rgb.shape
    rows = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(rows, 6)))
        fh.write(chunk(b"IEND", b""))


def _mcp_write_bmp(path, rgb):
    h, w, _ = rgb.shape
    pad = (-w * 3) % 4
    rows = []
    for y in range(h - 1, -1, -1):
        row = rgb[y].tobytes()
        rows.append(row + b"\x00" * pad)
    pixels = b"".join(rows)
    size = 14 + 40 + len(pixels)
    with open(path, "wb") as fh:
        fh.write(b"BM")
        fh.write(struct.pack("<IHHI", size, 0, 0, 54))
        fh.write(struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0,
                             len(pixels), 2835, 2835, 0, 0))
        fh.write(pixels)


def _mcp_shot(path, fmt, wanted_title):
    tabs = _mcp_list_windows()
    if not tabs:
        raise RuntimeError("no visible window found in the ANSA process")
    target = None
    if wanted_title:
        for item in tabs:
            if item["title"] == wanted_title:
                target = item
                break
        if target is None:
            raise RuntimeError("no window titled %r; available: %s"
                               % (wanted_title, [t["title"] for t in tabs]))
    else:
        # Prefer the MDI main window: biggest area whose title starts with ANSA.
        ranked = sorted(tabs, key=lambda t: t["width"] * t["height"], reverse=True)
        for item in ranked:
            if item["title"].lower().startswith("ansa"):
                target = item
                break
        if target is None:
            target = ranked[0]
    try:
        guitk.BCApplicationProcessNonUserInputEvents()
    except Exception:
        pass
    w, h, rgb = _mcp_grab_window(target["hwnd"])
    lit = float((rgb.sum(axis=2) > 30).mean())
    if fmt.lower() == "bmp":
        _mcp_write_bmp(path, rgb)
    else:
        _mcp_write_png(path, rgb)
    return {"file": path, "window": target["title"], "hwnd": target["hwnd"],
            "width": w, "height": h, "nonblack_ratio": round(lit, 3),
            "windows": [t["title"] for t in tabs]}
'''

_WINDOW_SHOT_TAIL = r'''
_mcp_result = _mcp_shot(__OUT__, __FMT__, __TITLE__)
_mcp_result
'''


def _window_shot_code(path: Path, image_format: str,
                      window_title: str = "") -> str:
    fmt = "bmp" if image_format.lower() in ("bmp",) else "png"
    return (_WINDOW_SHOT_CODE
            + _WINDOW_SHOT_TAIL
            .replace("__OUT__", repr(str(path)))
            .replace("__FMT__", repr(fmt))
            .replace("__TITLE__", repr(window_title)))


@mcp.tool()
def ansa_screenshot(path: str, image_format: str = "png",
                    timeout: int = 180, target: str = "viewport",
                    window_title: str = "") -> str:
    """Save a picture of the ANSA session - viewport or the whole window.

    Args:
        path: image output path (.png recommended).
        image_format: png | jpg | bmp | tif.
        target: "viewport" (3D graphics area only, fast, native SnapShot) or
              "window" (full GUI: menu bar, toolbars, model tree, side panels,
              console - captured through PrintWindow). Use "window" when you
              need to see *where the user is* in the interface, not just the
              geometry.
        window_title: only with target="window"; exact window title such as
              "ANSA v23.1.1". Empty means the largest visible ANSA window.

    Requires gui mode; batch mode has no window at all.
    """
    p, err = _resolve_out(path)
    if p is None:
        return f"**{err}**"
    if target.strip().lower() == "window":
        code = _window_shot_code(p, image_format, window_title.strip())
        return _fmt(SESSION.run(code, timeout=float(timeout),
                                tool="ansa_screenshot",
                                params={"path": str(p), "target": "window"}))
    code = f"""
try:
    base.RedrawAll()
except Exception:
    pass
ret = utils.SnapShot({p!r}, {image_format!r})
import os
{{'file': {p!r}, 'return': ret, 'exists': os.path.exists({p!r}),
  'size': os.path.getsize({p!r}) if os.path.exists({p!r}) else 0}}
"""
    return _fmt(SESSION.run(code, timeout=float(timeout), tool="ansa_screenshot"))


JOBS_DIR = LOG_DIR / "jobs"


def _write_job_state(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=repr),
                   encoding="utf-8")
    os.replace(tmp, path)


def _async_payload_code(code: str, log_path: Path) -> str:
    """Wrap user code so it runs inside ANSA with live, flushed output.

    ``print`` is rebound to a writer that flushes every line, so
    ``ansa_job_status`` can tail progress while the job is still running.
    ``pump()`` is injected for long loops that want the GUI to keep
    redrawing instead of freezing.

    Note on why this is not an ``after``-style queue like the HyperMesh MCP
    uses: ANSA has ``guitk.BCTimerSingleShot``, but a timer armed from an IAP
    script never fires once that script returns - verified on 23.1.1, the job
    stayed at "queued" indefinitely and only ran when something else called
    ``BCApplicationProcessNonUserInputEvents``. So the deferral has to happen
    on the MCP side (a worker thread), not inside ANSA.
    """
    return f"""
import io as _io, time as _time, traceback as _traceback

_mcp_log = {str(log_path)!r}
_mcp_src = {code!r}
_mcp_fh = _io.open(_mcp_log, "a", encoding="utf-8", errors="replace")


def _mcp_emit(*args, **kwargs):
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\\n")
    if sep is None:
        sep = " "
    _mcp_fh.write(sep.join(str(a) for a in args) + (end or ""))
    _mcp_fh.flush()


def pump():
    \"\"\"Let ANSA redraw and process input. Call it inside long loops.\"\"\"
    try:
        guitk.BCApplicationProcessNonUserInputEvents()
    except Exception:
        pass


globals()["print"] = _mcp_emit
_t0 = _time.time()
_status, _error = "ok", ""
try:
    exec(compile(_mcp_src, "<mcp-job>", "exec"), globals())
except BaseException:
    _status, _error = "error", _traceback.format_exc()
try:
    base.RedrawAll()
except Exception:
    pass
_mcp_fh.close()
{{"mcp_status": _status, "mcp_error": _error,
  "mcp_elapsed": round(_time.time() - _t0, 3)}}
"""


@mcp.tool()
def ansa_run_async(code: str, timeout: int = 3600,
                   mutating: bool = True) -> str:
    """Run python in ANSA without blocking the MCP client.

    Use this for anything that takes more than a few seconds: tetra meshing,
    batch mesh, STEP import, long loops. Plain ``ansa_run_python`` holds the
    IAP socket for the whole run, so the client waits, cannot see progress,
    and loses the result if it times out. Here:

    * the script is dispatched on a worker thread and this call returns a
      ``job_id`` immediately,
    * ``print()`` inside the job is flushed line by line into a log you can
      tail with ``ansa_job_status`` while it runs,
    * ``pump()`` is available inside the job; call it in long loops to let
      the GUI redraw (ANSA's own event loop does not run during an IAP
      script, so without it the window freezes until the job ends).

    Args:
        code: python statements, same namespace as ``ansa_run_python``
              (ansa, base, constants, mesh, utils, guitk, ansavars).
        timeout: how long the job itself may run before the server gives up.
        mutating: keep the default ``True`` unless you know the script only
              reads. It records the job in the operation ledger so a crashed
              job is reported as unresolved instead of being silently lost.
    """
    if not code.strip():
        return "**code is empty**"
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = (f"job_{time.strftime('%Y%m%d_%H%M%S')}_"
              f"{new_operation_id().split('_')[-1][:8]}")
    state_path = JOBS_DIR / f"{job_id}.json"
    log_path = JOBS_DIR / f"{job_id}.log"
    log_path.write_text("", encoding="utf-8")
    started = time.time()
    _write_job_state(state_path, {"job_id": job_id, "status": "queued",
                                  "queued_at": started,
                                  "mutating": mutating})
    payload = _async_payload_code(code, log_path)

    def _worker() -> None:
        try:
            res = SESSION.run(payload, timeout=float(timeout),
                              tool="ansa_run_async",
                              params={"job_id": job_id},
                              operation_id=job_id, mutating=mutating)
        except Exception as exc:  # noqa: BLE001
            _write_job_state(state_path, {
                "job_id": job_id, "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "queued_at": started, "finished_at": time.time()})
            return
        parsed: dict = {}
        try:
            parsed = json.loads(res.get("result") or "{}")
        except Exception:  # noqa: BLE001
            parsed = {}
        status = parsed.get("mcp_status") or ("ok" if res.get("ok") else "error")
        _write_job_state(state_path, {
            "job_id": job_id, "status": status,
            "elapsed": parsed.get("mcp_elapsed"),
            "error": parsed.get("mcp_error") or res.get("error", ""),
            "operation_id": res.get("operation_id", job_id),
            "delta": res.get("delta") or {},
            "queued_at": started, "finished_at": time.time(),
        })

    threading.Thread(target=_worker, name=f"ansa-job-{job_id}",
                     daemon=True).start()
    return (f"**dispatched** `{job_id}`\n\n"
            f"- log: `{log_path}`\n"
            f"- poll: `ansa_job_status(job_id=\"{job_id}\")`\n"
            f"- the client is free now; the job keeps running inside ANSA\n"
            f"- inside the job, call `pump()` in long loops so the window "
            f"keeps redrawing")


@mcp.tool()
def ansa_job_status(job_id: str = "", tail_lines: int = 40) -> str:
    """Read the status and live log tail of an ``ansa_run_async`` job.

    Args:
        job_id: id returned by ``ansa_run_async``. Empty lists recent jobs.
        tail_lines: how many trailing log lines to show (max 400).
    """
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if not job_id.strip():
        files = sorted(JOBS_DIR.glob("job_*.json"),
                       key=lambda p: p.stat().st_mtime)
        if not files:
            return "no async jobs have been run yet"
        lines = [f"**{len(files)} job(s), newest last**", "",
                 "| job_id | status | age |", "|---|---|---|"]
        for f in files[-20:]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {"status": "unreadable"}
            age = int(time.time() - (data.get("finished_at")
                                     or data.get("queued_at")
                                     or f.stat().st_mtime))
            lines.append(f"| `{f.stem}` | {data.get('status', '?')} | {age}s |")
        return "\n".join(lines)

    job = job_id.strip()
    state_path = JOBS_DIR / f"{job}.json"
    log_path = JOBS_DIR / f"{job}.log"
    if not state_path.exists() and not log_path.exists():
        return f"**unknown job** `{job}` - nothing under `{JOBS_DIR}`"
    try:
        data = (json.loads(state_path.read_text(encoding="utf-8"))
                if state_path.exists() else {})
    except Exception as exc:  # noqa: BLE001
        return f"**unreadable state** `{state_path}`: {exc}"

    status = data.get("status", "running")
    age = int(time.time() - (data.get("finished_at")
                             or data.get("queued_at") or time.time()))
    head = f"**{job}** - status: `{status}` - age: {age}s"
    if data.get("elapsed") is not None:
        head += f" - script elapsed: {data.get('elapsed')}s"
    out = [head]
    text = ""
    if log_path.exists():
        rows = log_path.read_text(encoding="utf-8",
                                  errors="replace").splitlines()
        n = max(1, min(int(tail_lines), 400))
        text = "\n".join(rows[-n:])
        out.append(f"- log: `{log_path}` ({len(rows)} line(s))")
    else:
        out.append("- log: (not created yet)")
    if text.strip():
        out += ["", "```", text.rstrip(), "```"]
    elif status in ("queued", "running"):
        out.append("(no output yet)")
    err = str(data.get("error") or "").strip()
    if err:
        out += ["", "**error**", "```", err[-4000:].rstrip(), "```"]
    return "\n".join(out)


@mcp.tool()
def ansa_gui_state() -> str:
    """Report where the user is in the interface, without changing anything.

    Returns the active module menu, whether the session is really gui mode,
    the current deck, and every visible top-level window of the ANSA process
    (dialogs included). Pair it with ``ansa_screenshot(target="window")``.
    """
    code = r'''
import ctypes, os
from ctypes import wintypes


def _mcp_windows():
    u = ctypes.windll.user32
    own = os.getpid()
    names = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own:
            rect = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rect))
            names.append({"title": buf.value,
                          "width": rect.right - rect.left,
                          "height": rect.bottom - rect.top})
        return True

    u.EnumWindows(proc(_cb), 0)
    return names


_state = {}
try:
    _state["gui_mode"] = bool(guitk.BCApplicationIsGuiMode())
except Exception as _exc:
    _state["gui_mode"] = "ERR: %s" % _exc
try:
    _state["menu"] = base.CurrentMenu()
except Exception as _exc:
    _state["menu"] = "ERR: %s" % _exc
try:
    _state["deck"] = str(base.CurrentDeck())
except Exception as _exc:
    _state["deck"] = "ERR: %s" % _exc
_state["windows"] = _mcp_windows()
_state
'''
    res = SESSION.run(code, tool="ansa_gui_state")
    if not res.get("ok"):
        return _fmt(res)
    try:
        data = json.loads(res.get("result") or "{}")
    except Exception:  # noqa: BLE001
        return _fmt(res)
    lines = ["| item | value |", "|---|---|",
             f"| gui mode | {data.get('gui_mode')} |",
             f"| module menu | `{data.get('menu')}` |",
             f"| deck | `{data.get('deck')}` |"]
    lines += ["", "**visible windows**", "", "| title | size |", "|---|---|"]
    for w in data.get("windows") or []:
        lines.append(f"| {w.get('title')} | {w.get('width')}x{w.get('height')} |")
    lines += ["", "switch module with `ansa_gui_menu`, capture with "
                  "`ansa_screenshot(target=\"window\")`"]
    return "\n".join(lines)


@mcp.tool()
def ansa_gui_menu(menu: str = "") -> str:
    """Read or switch the ANSA module menu (the first toolbar row).

    Args:
        menu: TOPO | MESH | VOLUME MESH | MORPH | HEXA BLOCK | CROSS |
              KINETICS. Empty just reports the current one.

    This is a real GUI state change - the toolbar and the right-hand function
    panel are rebuilt. It does not touch the model, so it is not recorded in
    the operation ledger.
    """
    menu = menu.strip()
    if menu:
        code = f"""
_before = base.CurrentMenu()
_ret = base.SetCurrentMenu({menu!r})
try:
    guitk.BCApplicationProcessNonUserInputEvents()
except Exception:
    pass
{{'requested': {menu!r}, 'return': _ret, 'before': _before,
  'after': base.CurrentMenu()}}
"""
    else:
        code = "{'before': base.CurrentMenu(), 'after': base.CurrentMenu()}"
    res = SESSION.run(code, tool="ansa_gui_menu", params={"menu": menu})
    if not res.get("ok"):
        return _fmt(res)
    try:
        data = json.loads(res.get("result") or "{}")
    except Exception:  # noqa: BLE001
        return _fmt(res)
    if not menu:
        return f"current module menu: `{data.get('after')}`"
    if data.get("return") in (1, "1", True):
        return (f"switched `{data.get('before')}` -> `{data.get('after')}`\n\n"
                f"capture it with `ansa_screenshot(target=\"window\")`")
    return (f"**switch refused** (`{data.get('before')}` unchanged) - "
            f"`{menu}` is not an accepted menu name. Try TOPO, MESH, "
            f"VOLUME MESH, MORPH, HEXA BLOCK, CROSS or KINETICS.")


@mcp.tool()
def ansa_step_history(limit: int = 20) -> str:
    """List what this server has done recently, newest last.

    Each entry shows the tool, its operation_id, the outcome, and how the
    entity counts changed. Use it after an interruption to see which step is
    unresolved before making further changes.

    Args:
        limit: how many entries to return (max 100, this session only).
    """
    if not STEPS:
        return "no steps recorded yet in this server session"
    rows = list(STEPS)[-max(1, min(int(limit), 100)):]
    lines = [f"**{len(rows)} step(s), this session only**", "",
             "| # | time | tool | status | model change | operation_id |",
             "|---|---|---|---|---|---|"]
    for s in rows:
        delta = _fmt_delta(s.get("delta") or {}) if s.get("mutating") else "—"
        lines.append(
            f"| {s['seq']} | {s['at']} | `{s['tool']}` | {s['status']} | "
            f"{delta} | `{s['operation_id']}` |"
        )
    unresolved = [s for s in rows if s["status"] == "OUTCOME_UNKNOWN"]
    if unresolved:
        lines += ["", "⚠ **unresolved steps** — inspect the model before "
                      "continuing: " + ", ".join(f"`{s['operation_id']}`"
                                                 for s in unresolved)]
    return "\n".join(lines)


@mcp.tool()
def ansa_api_search(query: str, module: str = "", top_n: int = 5) -> str:
    """Search the ANSA python API reference (name + signature + example).

    ANSA's python bindings expose no docstrings, so use this before guessing
    a function's arguments.

    Args:
        query: english keywords, e.g. "create node", "shell thickness",
              "transform translate", "quality check".
        module: optional filter, e.g. "ansa.base", "ansa.mesh", "ansa.batchmesh".
        top_n: number of hits.
    """
    index = _api_index_path()
    if index is None:
        return ("ANSA API index not found. Install it with "
                "`pip install git+https://github.com/<ansa-api-mcp>` or set "
                "`ANSA_API_INDEX` to the JSON file path.")
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot read API index: {exc}"
    funcs = data.get("functions", [])
    tokens = [t for t in re.split(r"[^0-9a-zA-Z_]+", query.lower()) if t]
    scored: list[tuple[int, dict]] = []
    for f in funcs:
        if module and module not in f.get("module", ""):
            continue
        hay = " ".join([
            f.get("name", ""), f.get("description", ""),
            f.get("signature", ""), " ".join(f.get("keywords", []) or []),
        ]).lower()
        score = sum(3 if t in f.get("name", "").lower() else 1
                    for t in tokens if t in hay)
        if score:
            scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return f"no API matches for `{query}`" + (f" in {module}" if module else "")
    out = [f"**{len(scored)} match(es) for `{query}`** (showing {min(top_n, len(scored))})\n"]
    for score, f in scored[:top_n]:
        out.append(f"### `{f.get('signature', f.get('name'))}`")
        desc = (f.get("description") or "").strip().replace("\n", " ")
        if desc:
            out.append(desc[:400])
        ex = (f.get("examples") or "").strip()
        if ex:
            snippet = "\n".join(ex.splitlines()[:18]).replace("/n", "")
            out.append("```python\n" + snippet + "\n```")
        out.append("")
    return "\n".join(out)


def _api_index_path() -> Path | None:
    env = os.environ.get("ANSA_API_INDEX")
    if env and Path(env).exists():
        return Path(env)
    candidates = [
        SERVER_DIR / "tools" / "ansa_api_index.json",        # shipped with the repo
        SERVER_DIR.parent / "tools" / "ansa_api_index.json",
        Path.home() / "AppData/Roaming/Python/Python311/site-packages/tools/ansa_api_index.json",
        Path.home() / "AppData/Roaming/Python/Python310/site-packages/tools/ansa_api_index.json",
        Path.home() / "AppData/Roaming/Python/Python39/site-packages/tools/ansa_api_index.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"ANSA home: {ANSA_HOME}")
    mcp.run()


if __name__ == "__main__":
    main()
