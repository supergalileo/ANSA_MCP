"""Idempotent operation ledger for the ANSA MCP server.

Why this exists
---------------
A tool call can fail *after* ANSA has already started changing the model: the
client timeout fires while a batch-mesh call is still running, the socket drops
mid-response, or the MCP client itself gets interrupted. Retrying such a call
blindly applies the change a second time -- a silent, cumulative error that is
very hard to spot on a large model (elements get meshed twice, a translate is
applied twice, entities are deleted twice).

Every mutating tool therefore carries an ``operation_id``. The ledger records
the claim *before* anything is sent to ANSA and the outcome afterwards, so a
retry is answered from disk instead of being re-executed:

===================  =======================================================
recorded state       what a retry with the same id does
===================  =======================================================
``success``          returns the stored summary, ANSA is not touched
``pending``          refuses with OUTCOME_UNKNOWN, ANSA is not touched
``unknown``          refuses with OUTCOME_UNKNOWN, ANSA is not touched
``failed``           allowed to run again (nothing was confirmed as applied)
different params     refuses: the id already belongs to another operation
===================  =======================================================

Records survive an MCP server restart because they live next to the session
state file. The ledger is bounded: when it is full, new writes are refused
rather than evicting a record that might still matter.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

MAX_RECORDS = 256
MAX_AGE_SECONDS = 7 * 24 * 3600.0
FAILED_RETENTION_SECONDS = 3600.0
MAX_STORED_TEXT = 4000

PENDING = "pending"
SUCCESS = "success"
FAILED = "failed"
UNKNOWN = "unknown"


class LedgerError(RuntimeError):
    """Base class for refusals raised by the ledger."""


class OperationConflict(LedgerError):
    """The id was already used for a different operation."""


class OperationOutcomeUnknown(LedgerError):
    """The operation may still be applying; it must not be run again."""


class LedgerFull(LedgerError):
    """Too many live records; refuse rather than evict something that matters."""


def new_operation_id() -> str:
    """A short, unguessable id that is safe to paste into logs."""
    return "op_" + uuid.uuid4().hex[:20]


def fingerprint(tool: str, params: dict[str, Any]) -> str:
    """Stable digest of everything that defines 'this exact operation'."""
    body = json.dumps(
        {"tool": tool, "params": params},
        sort_keys=True,
        ensure_ascii=False,
        default=repr,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


class OperationLedger:
    """On-disk record of in-flight and completed mutating operations."""

    def __init__(self, path: Path, max_records: int = MAX_RECORDS,
                 max_age: float = MAX_AGE_SECONDS) -> None:
        self.path = Path(path)
        self.max_records = int(max_records)
        self.max_age = float(max_age)
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._records = {}
            return
        records = raw.get("records") if isinstance(raw, dict) else None
        self._records = records if isinstance(records, dict) else {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps({"version": 1, "records": self._records},
                           indent=1, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            # A broken ledger must not take the whole server down; the
            # in-memory view still protects this session.
            pass

    def _prune(self) -> None:
        now = time.time()
        drop: list[str] = []
        for op_id, rec in self._records.items():
            status = rec.get("status")
            updated = float(rec.get("updated_at") or rec.get("created_at") or 0)
            age = now - updated
            if status in (PENDING, SUCCESS, UNKNOWN):
                if age > self.max_age:
                    drop.append(op_id)
            elif status == FAILED and age > FAILED_RETENTION_SECONDS:
                # A clean failure never changed the model, so forgetting it is
                # safe and keeps room for real records.
                drop.append(op_id)
        for op_id in drop:
            self._records.pop(op_id, None)

    # -- public api --------------------------------------------------------
    def reserve(self, operation_id: str, tool: str, fp: str) -> dict[str, Any] | None:
        """Claim ``operation_id`` before touching ANSA.

        Returns the stored record when an identical operation already
        succeeded -- the caller should replay it instead of running again.
        Raises :class:`OperationConflict` for id reuse and
        :class:`OperationOutcomeUnknown` for unresolved records.
        """
        self._prune()
        record = self._records.get(operation_id)
        if record is not None:
            if record.get("fingerprint") != fp:
                raise OperationConflict(
                    f"operation_id `{operation_id}` was already used for a "
                    f"different {record.get('tool', '?')} call. Use a new id "
                    "for a genuinely different operation."
                )
            status = record.get("status")
            if status == SUCCESS:
                return record
            if status in (PENDING, UNKNOWN):
                raise OperationOutcomeUnknown(
                    f"operation_id `{operation_id}` is recorded as `{status}`: "
                    "ANSA may have already applied it, or may still be applying "
                    "it. Read the model back (ansa_model_info / "
                    "ansa_get_entity_values) before deciding what to do. Do not "
                    "rerun it with a fresh id."
                )
            # FAILED never reached the model, so falling through re-runs it.

        if record is None and len(self._records) >= self.max_records:
            raise LedgerFull(
                f"operation ledger holds {len(self._records)} records "
                f"(limit {self.max_records}). Delete {self.path.name} to start "
                "a new ledger only after confirming no operation is unresolved."
            )

        now = time.time()
        self._records[operation_id] = {
            "tool": tool,
            "fingerprint": fp,
            "status": PENDING,
            "created_at": record.get("created_at", now) if record else now,
            "updated_at": now,
            "attempts": (record.get("attempts", 0) if record else 0) + 1,
        }
        self._save()
        return None

    def finish(self, operation_id: str, status: str, *,
               summary: str = "", error: str = "", elapsed: float | None = None,
               extra: dict[str, Any] | None = None) -> None:
        record = self._records.get(operation_id)
        if record is None:
            return
        record["status"] = status
        record["updated_at"] = time.time()
        if summary:
            record["summary"] = summary[:MAX_STORED_TEXT]
        if error:
            record["error"] = error[:MAX_STORED_TEXT]
        if elapsed is not None:
            record["elapsed"] = round(float(elapsed), 2)
        if extra:
            record["extra"] = extra
        self._save()

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self._records.get(operation_id)

    def pending_count(self) -> int:
        return sum(1 for r in self._records.values()
                   if r.get("status") in (PENDING, UNKNOWN))

    def size(self) -> int:
        return len(self._records)
