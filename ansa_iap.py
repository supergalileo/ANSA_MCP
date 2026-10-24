"""IAP (Inter ANSA Protocol) client.

ANSA ships an official remote-control protocol: start ANSA with
``-listenport <port>`` and it listens on TCP for script-execution requests.

The bundled reference implementation
(``<ANSA>/scripts/RemoteControl/ansa/AnsaProcessModule.py``) has a few
problems that matter for a long-lived MCP server:

* ``recv()`` assumes one ``socket.recv`` call returns a whole chunk and
  computes the remaining length as ``length`` instead of ``length - 16``.
* ``InformationElement.frombytes`` references an undefined ``length`` name
  for string tags.
* there is no handshake retry, yet ANSA opens the listen port several
  seconds *before* the protocol is ready (very visible in GUI mode).

This module re-implements the wire format with correct stream handling.
"""
from __future__ import annotations

import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------- tags --
TAG_RESULT_CODE = 0x01
TAG_PROCESS_ID = 0x02
TAG_SCRIPT_STRING = 0x03
TAG_ENTRY_METHOD = 0x04
TAG_SCRIPT_RETURN_TYPE = 0x05
TAG_SCRIPT_RETVAL_STRING_DICT = 0x06
TAG_SCRIPT_EXECUTION_DETAILS = 0x07
TAG_SUPPORTED_SERVICE = 0x08
TAG_POST_CONNECTION_ACTION = 0x09
TAG_PRE_EXECUTION_DB_ACTION = 0x0A
TAG_SCRIPT_RETVAL_BYTES = 0x0B
TAG_MUTED_EXECUTION = 0x0E

_INT_TAGS = {
    TAG_RESULT_CODE, TAG_PROCESS_ID, TAG_SCRIPT_RETURN_TYPE,
    TAG_SCRIPT_EXECUTION_DETAILS, TAG_SUPPORTED_SERVICE,
    TAG_POST_CONNECTION_ACTION, TAG_PRE_EXECUTION_DB_ACTION,
    TAG_MUTED_EXECUTION,
}
_STR_TAGS = {TAG_SCRIPT_STRING, TAG_ENTRY_METHOD}

# service / message codes
SVC_COMMON = 0x00
SVC_SCRIPT_EXECUTION = 0x01
MSG_HELLO = 0x01
MSG_EXECUTE_SCRIPT = 0x02
MSG_GOODBYE = 0x03
TYPE_REQUEST = 0x01

# enums
RESULT_SUCCESS = 0x00
PRE_EXEC_KEEP_DATABASE = 0x01
PRE_EXEC_RESET_DATABASE = 0x00
POST_KEEP_LISTENING = 0x01
POST_SHUT_DOWN = 0x00
MUTED_OFF = 0x00

RETURN_TYPE_NONE = 0x00
RETURN_TYPE_UNSUPPORTED = 0x01
RETURN_TYPE_STRING_DICT = 0x02
RETURN_TYPE_BYTES = 0x03

EXEC_DETAIL_TEXT = {
    0x00: "success",
    0x01: "temp_file_failed",
    0x02: "exception_raised",
    0x03: "script_not_loaded",
    0x04: "unknown_error",
}
RESULT_CODE_TEXT = {
    0x00: "success",
    0x01: "internal_error",
    0x02: "operation_error",
    0x03: "no_compatible_services",
    None: "no_result_code",
}


class IapError(RuntimeError):
    """Raised when the IAP transport or protocol fails."""


class IapOutcomeUnknown(IapError):
    """The script was handed to ANSA but no determinate result came back.

    This is the dangerous failure mode for a mutating call. ANSA runs the
    script synchronously on its own thread and cannot be cancelled once it has
    started, so a timeout or a dropped connection here means the model *may*
    already have been changed. Callers must inspect the model rather than
    resending the same script.

    A failure raised while *sending* is deliberately not classified this way:
    the message is a few hundred bytes over loopback, so a send error means the
    socket was already dead and ANSA received nothing.
    """


# ---------------------------------------------------------------- encoding ---
def _padding(length: int) -> int:
    return (4 - length % 4) % 4


def _pack_ie(tag: int, value: Any) -> bytes:
    if isinstance(value, int):
        return struct.pack(">LLI", tag, 12, value)
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    head = struct.pack(">LL", tag, 8 + len(raw))
    return head + raw + b"\xa5" * _padding(len(raw))


def _pack_header(service: int, code: int, txn: int, total_len: int) -> bytes:
    return struct.pack(">BBHLLL", 1, TYPE_REQUEST, service, code, txn, total_len)


def _decode_ies(body: bytes) -> list[tuple[int, Any]]:
    """Decode a run of TLVs from a message body."""
    ies: list[tuple[int, Any]] = []
    pos = 0
    while pos + 8 <= len(body):
        tag, ie_len = struct.unpack(">LL", body[pos:pos + 8])
        if ie_len < 8 or pos + ie_len > len(body):
            break
        value_bytes = body[pos + 8:pos + ie_len]
        if tag in _INT_TAGS:
            value: Any = struct.unpack(">L", value_bytes[-4:])[0] if len(value_bytes) >= 4 else None
        elif tag in _STR_TAGS:
            value = value_bytes.decode("utf-8", "replace")
        elif tag == TAG_SCRIPT_RETVAL_STRING_DICT:
            # keep the full IE (incl. 8-byte header): decode_string_dict() slices [8:]
            value = body[pos:pos + ie_len]
        else:
            value = value_bytes
        ies.append((tag, value))
        pos += ie_len + _padding(ie_len - 8)
    return ies


def decode_string_dict(raw: bytes) -> dict[str, str]:
    """Decode ``Tag.script_retval_string_dict`` into a python dict."""
    if len(raw) < 12:
        return {}
    payload = raw[8:]
    count = struct.unpack(">L", payload[:4])[0]
    pos = 4
    out: dict[str, str] = {}
    for _ in range(count):
        if pos + 4 > len(payload):
            break
        klen = struct.unpack(">L", payload[pos:pos + 4])[0]
        pos += 4
        key = payload[pos:pos + klen].decode("utf-8", "replace")
        pos += klen
        if pos + 4 > len(payload):
            break
        dlen = struct.unpack(">L", payload[pos:pos + 4])[0]
        pos += 4
        out[key] = payload[pos:pos + dlen].decode("utf-8", "replace")
        pos += dlen
    return out


# ----------------------------------------------------------------- result ----
@dataclass
class ScriptResult:
    """Outcome of one remote script execution."""

    result_code: int | None = None
    exec_details: int | None = None
    return_type: int | None = None
    payload: dict[str, str] = field(default_factory=dict)
    raw_bytes: bytes | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.result_code == RESULT_SUCCESS

    def describe(self) -> str:
        parts = [
            f"result={RESULT_CODE_TEXT.get(self.result_code, hex(self.result_code or 0))}",
        ]
        if self.exec_details is not None:
            parts.append(f"exec={EXEC_DETAIL_TEXT.get(self.exec_details, self.exec_details)}")
        return ", ".join(parts)


# ------------------------------------------------------------------ client ---
class IapClient:
    """One TCP connection to an ANSA process running in listener mode."""

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 300.0):
        self.port = port
        self.host = host
        self.timeout = timeout
        self._txn = 0
        try:
            self._sock = socket.create_connection((host, port), timeout=min(timeout, 30.0))
        except OSError as exc:
            raise IapError(f"cannot connect to ANSA on {host}:{port}: {exc}") from exc
        self._sock.settimeout(timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # -- transport ---------------------------------------------------------
    def _next_txn(self) -> int:
        self._txn += 1
        return self._txn

    def _send(self, service: int, code: int, ies: Iterable[tuple[int, Any]]) -> None:
        payload = b"".join(_pack_ie(t, v) for t, v in ies)
        msg = _pack_header(service, code, self._next_txn(), 16 + len(payload)) + payload
        try:
            self._sock.sendall(msg)
        except OSError as exc:
            raise IapError(f"send failed: {exc}") from exc

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout as exc:
                raise IapOutcomeUnknown(
                    f"timed out waiting for ANSA after {self.timeout:.0f}s "
                    "(the script may still be running inside ANSA)"
                ) from exc
            if not chunk:
                raise IapOutcomeUnknown(
                    "ANSA closed the connection before answering"
                )
            buf += chunk
        return bytes(buf)

    def _recv_message(self) -> list[tuple[int, Any]]:
        header = self._recv_exact(16)
        _ver, _flags, _service, _code, _txn, total_len = struct.unpack(">BBHLLL", header)
        if total_len < 16 or total_len > 512 * 1024 * 1024:
            raise IapError(f"implausible message length {total_len}")
        body = self._recv_exact(total_len - 16)
        return _decode_ies(body)

    # -- protocol ----------------------------------------------------------
    def hello(self) -> int | None:
        self._send(SVC_COMMON, MSG_HELLO, [
            (TAG_PROCESS_ID, os.getpid()),
            (TAG_SUPPORTED_SERVICE, 0x00010002),
        ])
        ies = dict(self._recv_message())
        return ies.get(TAG_RESULT_CODE)

    def execute(self, script_text: str, entry: str | None = None,
                keep_database: bool = True, timeout: float | None = None) -> ScriptResult:
        t0 = time.time()
        if timeout is not None:
            self._sock.settimeout(timeout)
        ies: list[tuple[int, Any]] = [(TAG_SCRIPT_STRING, script_text)]
        if entry:
            ies.append((TAG_ENTRY_METHOD, entry))
        ies.append((TAG_PRE_EXECUTION_DB_ACTION,
                    PRE_EXEC_KEEP_DATABASE if keep_database else PRE_EXEC_RESET_DATABASE))
        self._send(SVC_SCRIPT_EXECUTION, MSG_EXECUTE_SCRIPT, ies)
        decoded = dict(self._recv_message())
        res = ScriptResult(
            result_code=decoded.get(TAG_RESULT_CODE),
            exec_details=decoded.get(TAG_SCRIPT_EXECUTION_DETAILS),
            return_type=decoded.get(TAG_SCRIPT_RETURN_TYPE),
            elapsed=time.time() - t0,
        )
        if TAG_SCRIPT_RETVAL_STRING_DICT in decoded:
            res.payload = decode_string_dict(decoded[TAG_SCRIPT_RETVAL_STRING_DICT])
        elif TAG_SCRIPT_RETVAL_BYTES in decoded:
            res.raw_bytes = decoded[TAG_SCRIPT_RETVAL_BYTES]
        return res

    def goodbye(self, keep_listening: bool = True) -> int | None:
        self._send(SVC_COMMON, MSG_GOODBYE, [
            (TAG_POST_CONNECTION_ACTION, POST_KEEP_LISTENING if keep_listening else POST_SHUT_DOWN),
        ])
        ies = dict(self._recv_message())
        return ies.get(TAG_RESULT_CODE)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def connect_with_retry(port: int, host: str = "127.0.0.1", timeout: float = 300.0,
                       deadline: float = 240.0, quiet: bool = True) -> IapClient:
    """Connect + handshake, retrying while ANSA finishes booting.

    ANSA binds the listen port before the IAP handler is ready; connecting
    too early yields a reset (WinError 10054) or a failed handshake.
    """
    t0 = time.time()
    last = "no attempt"
    while time.time() - t0 < deadline:
        client: IapClient | None = None
        try:
            client = IapClient(port, host, timeout=timeout)
            code = client.hello()
            if code == RESULT_SUCCESS:
                return client
            last = f"handshake result_code={code}"
            client.close()
        except (IapError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if client is not None:
                client.close()
        if not quiet:
            print(f"[iap] retry: {last}")
        time.sleep(2.0)
    raise IapError(f"IAP handshake with ANSA on port {port} failed after "
                   f"{deadline:.0f}s: {last}")
