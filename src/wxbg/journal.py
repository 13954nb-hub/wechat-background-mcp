"""Durable, cross-process reservations for mutations that must not be retried.

Call reserve, then begin immediately before the first possible side effect. Only
the reservation owner may complete or mark_unknown. A process crash after begin
leaves an executing row: future reserve calls raise OutcomeUnknown indefinitely.
There is deliberately no lease expiry, takeover, deletion, or automatic retry.

Arguments are stored only as a canonical SHA-256 digest. Results must be small
metadata summaries; opaque refs must never contain message text or draft text.
Keep the database on a local disk, and include account identity in ``args``.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any, Iterator


class JournalError(Exception):
    code = "journal_error"


class Conflict(JournalError):
    code = "operation_conflict"


class Busy(JournalError):
    code = "operation_busy"


class OutcomeUnknown(JournalError):
    code = "outcome_unknown"


class InvalidOperationId(JournalError, ValueError):
    code = "invalid_operation_id"


class InvalidPayload(JournalError, ValueError):
    code = "invalid_payload"


class InvalidResult(JournalError, ValueError):
    code = "invalid_result_summary"


class InvalidTransition(JournalError):
    code = "invalid_operation_transition"


class StorageError(JournalError):
    code = "journal_storage_error"


@dataclass(frozen=True)
class Reservation:
    operation_id: str
    token: str | None
    replay: bool
    result: dict[str, Any] | None = None


_CODE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z")
_REF = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:@/+\-=]{0,255}\Z")
_REF_KEYS = frozenset({
    "account", "conversation", "message", "attachment", "file", "operation",
    "account_ref", "conversation_ref", "message_ref", "attachment_ref", "file_ref",
})
_SUMMARY_KEYS = frozenset({
    "status", "verification_level", "counts", "refs", "error_code",
    "reason_code", "duration_ms", "background_mode", "ok",
    "submitted", "remote_receipt_verified", "upload_status",
})
_UPLOAD_STATUSES = frozenset({"uploading", "completed", "interrupted", "failed", "unknown"})


def _operation_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or "\x00" in value:
        raise InvalidOperationId("operation_id must contain 1 to 128 non-NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise InvalidOperationId("operation_id must be valid UTF-8") from exc


def _json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise InvalidPayload("arguments must contain only finite JSON values and string keys")


def canonical_payload_hash(action: str, args: dict[str, Any]) -> str:
    """Hash sorted, compact UTF-8 JSON without Unicode normalization."""
    if not isinstance(action, str) or not _CODE.fullmatch(action):
        raise InvalidPayload("action must be a nonempty action identifier of at most 128 characters")
    if not isinstance(args, dict):
        raise InvalidPayload("args must be a JSON object")
    try:
        _json_value(args)
        canonical = json.dumps(
            {"action": action, "args": args}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidPayload("arguments cannot be encoded as canonical UTF-8 JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def _summary_json(result: dict[str, Any]) -> str:
    if not isinstance(result, dict) or not result.keys() <= _SUMMARY_KEYS:
        raise InvalidResult("result must contain only documented metadata fields")
    for key, value in result.items():
        if key in ("ok", "submitted", "remote_receipt_verified"):
            valid = type(value) is bool
        elif key == "upload_status":
            valid = type(value) is str and value in _UPLOAD_STATUSES
        elif key == "duration_ms":
            valid = type(value) in (int, float) and 0 <= value <= 2 ** 63 - 1 and math.isfinite(value)
        elif key == "counts":
            valid = isinstance(value, dict) and all(
                isinstance(name, str) and _CODE.fullmatch(name)
                and type(count) is int and count >= 0
                for name, count in value.items()
            )
        elif key == "refs":
            valid = isinstance(value, dict) and all(
                name in _REF_KEYS and isinstance(ref, str) and _REF.fullmatch(ref)
                for name, ref in value.items()
            )
        else:
            valid = isinstance(value, str) and _CODE.fullmatch(value)
        if not valid:
            raise InvalidResult("invalid metadata value in result summary")
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 16384:
            raise InvalidResult("result summary exceeds 16 KiB")
        return encoded
    except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
        raise InvalidResult("result summary cannot be encoded") from exc


class Journal:
    """Each method owns a short SQLite transaction; no connection is shared."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0):
        if str(path) == ":memory:":
            raise ValueError("Journal requires a durable local database path")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        self.path = Path(path).resolve()
        self.timeout = timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise StorageError("unsupported journal schema version")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    token TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','executing','complete','outcome_unknown')),
                    result_json TEXT,
                    reason_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            connection.execute("PRAGMA user_version = 1")

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.DatabaseError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise Busy("operation journal is locked") from exc
            raise StorageError("operation journal could not be read or committed") from exc
        finally:
            if connection is not None:
                connection.close()  # Rolls back any uncommitted changes, including exceptions.

    @staticmethod
    def _owned(connection: sqlite3.Connection, operation_id: str, token: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
        if row is None or not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(row["token"], token):
            raise InvalidTransition("operation does not exist or reservation token does not match")
        return row

    def reserve(self, action: str, args: dict[str, Any], operation_id: str) -> Reservation:
        """Reserve once, replay a completed summary, or reject without mutation."""
        _operation_id(operation_id)
        digest = canonical_payload_hash(action, args)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if row is not None:
                if row["payload_hash"] != digest or row["action"] != action:
                    raise Conflict("operation_id was already used with different input")
                if row["state"] == "complete":
                    return Reservation(operation_id, None, True, json.loads(row["result_json"]))
                if row["state"] == "reserved":
                    raise Busy("operation is reserved; automatic takeover is disabled")
                raise OutcomeUnknown("operation may have executed; automatic retry is disabled")
            token, now = secrets.token_hex(32), time.time()
            connection.execute(
                "INSERT INTO operations (operation_id,action,payload_hash,token,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (operation_id, action, digest, token, "reserved", now, now),
            )
        return Reservation(operation_id, token, False)

    def begin(self, operation_id: str, token: str) -> None:
        """Durably mark uncertainty before permitting any external side effect."""
        _operation_id(operation_id)
        with self._transaction() as connection:
            row = self._owned(connection, operation_id, token)
            if row["state"] in ("executing", "outcome_unknown"):
                raise OutcomeUnknown("operation already began; it must not execute again")
            if row["state"] != "reserved":
                raise InvalidTransition("only a reserved operation can begin")
            connection.execute("UPDATE operations SET state='executing', updated_at=? WHERE operation_id=?", (time.time(), operation_id))

    def complete(self, operation_id: str, token: str, result: dict[str, Any]) -> dict[str, Any]:
        """Commit an immutable metadata summary; return the persistable summary."""
        _operation_id(operation_id)
        encoded = _summary_json(result)
        with self._transaction() as connection:
            row = self._owned(connection, operation_id, token)
            if row["state"] == "outcome_unknown":
                raise OutcomeUnknown("unknown outcome requires independent reconciliation")
            if row["state"] == "complete" and row["result_json"] == encoded:
                return json.loads(encoded)
            if row["state"] != "executing":
                raise InvalidTransition("only an executing operation can complete; summaries are immutable")
            connection.execute("UPDATE operations SET state='complete', result_json=?, updated_at=? WHERE operation_id=?", (encoded, time.time(), operation_id))
        return json.loads(encoded)

    def mark_unknown(self, operation_id: str, token: str, reason_code: str) -> None:
        """Record uncertainty permanently, including a cancelled reserved action."""
        _operation_id(operation_id)
        if not isinstance(reason_code, str) or not _CODE.fullmatch(reason_code):
            raise InvalidResult("reason_code must be an opaque code of at most 128 characters")
        with self._transaction() as connection:
            row = self._owned(connection, operation_id, token)
            if row["state"] == "complete":
                raise InvalidTransition("completed operations cannot become unknown")
            if row["state"] != "outcome_unknown":
                connection.execute("UPDATE operations SET state='outcome_unknown', reason_code=?, updated_at=? WHERE operation_id=?", (reason_code, time.time(), operation_id))

    def get(self, operation_id: str) -> dict[str, Any] | None:
        """Return semantic status metadata, never the token or arguments.

        Legacy pre-worker rejections are durably stored as completed decisions.
        Expose their rejected outcome directly so a caller cannot mistake the
        storage transition for a successful client mutation.
        """
        _operation_id(operation_id)
        with self._transaction(write=False) as connection:
            row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            summary = json.loads(row["result_json"]) if row["result_json"] is not None else None
            state, reason = row["state"], row["reason_code"]
            if (state == "complete" and isinstance(summary, dict)
                    and summary.get("ok") is False and summary.get("status") == "rejected"
                    and isinstance(summary.get("error_code"), str)):
                state, reason = "rejected", summary["error_code"]
            return {
                "operation_id": row["operation_id"], "action": row["action"],
                "payload_hash": row["payload_hash"], "state": state,
                "result": summary,
                "reason_code": reason, "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
