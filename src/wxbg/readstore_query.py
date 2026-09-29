"""微信背景只讀 SQLite 查詢核心。

本模組只接受呼叫方已開啟的 ``sqlite3.Connection``，只執行參數化的
``SELECT``/schema 檢查，不負責找檔案、取 key、解密、開啟資料庫或操作
微信 UI。訊息分片以呼叫方提供的 ``shard_id`` 命名空間隔離；``local_id``
從不被當成跨分片的全局 ID。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import heapq
import json
import math
import re
import sqlite3
import time
from typing import Any

from .readstore_body import MAX_INPUT_BYTES as MAX_BODY_INPUT_BYTES
from .readstore_body import MAX_OUTPUT_BYTES as MAX_BODY_OUTPUT_BYTES
from .readstore_body import decode_body
from .sender_role import unavailable_sender_role


MAX_LIMIT = 100
MAX_TEXT_CHARS = 4096
MAX_TEXT_BYTES = MAX_TEXT_CHARS * 4
MAX_KEYWORD_CHARS = 256
MAX_SHARDS = 64
MAX_SQL_STEPS = 100_000
MAX_QUERY_SECONDS = 5.0
MAX_OUTPUT_PAGE_BYTES = 384 * 1024 - 256  # reserve the public account-epoch field
_MIN_SQL_INT = -(1 << 63)
_MAX_SQL_INT = (1 << 63) - 1
_CURSOR_VERSION = 2
_INCREMENTAL_CURSOR_VERSION = 3
_PROGRESS_GRANULARITY = 1
_MAX_USERNAME_BYTES = MAX_TEXT_CHARS
_MAX_MESSAGE_RAW_BYTES = MAX_BODY_INPUT_BYTES
_MAX_MESSAGE_OUTPUT_BYTES = MAX_BODY_OUTPUT_BYTES
_MESSAGE_COMPRESSION_COLUMN = "WCDB_CT_message_content"
_MD5_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHARD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_SESSION_TABLE = "SessionTable"
_MESSAGE_COLUMNS = frozenset(
    {
        "local_id",
        "local_type",
        "real_sender_id",
        "create_time",
        "message_content",
        "server_id",
        "sort_seq",
    }
)
_SESSION_COLUMNS = frozenset(
    {
        "username",
        "unread_count",
        "summary",
        "last_timestamp",
        "is_hidden",
        "sort_timestamp",
    }
)


class ReadStoreError(ValueError):
    """查詢核心的固定、無正文錯誤。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InvalidQuery(ReadStoreError):
    """輸入或游標不符合有界查詢契約。"""


class UnsupportedSchema(ReadStoreError):
    """已提供的 connection 缺少本核心所需 schema。"""


@dataclass(frozen=True, slots=True)
class MessageShard:
    """一份呼叫方已開啟的訊息 connection 與其身份 namespace。"""

    shard_id: str
    connection: sqlite3.Connection


@dataclass(frozen=True, slots=True)
class MessageCursor:
    """跨分片穩定分頁所需的複合游標。

    ``sort_seq``、``shard_id``、``local_id``、``rowid`` 共同形成查詢內的
    邊界。游標同時綁定 account epoch、聊天、方向、filter 和完整分片集合；
    ``shard_id`` 是必要身份 namespace，``local_id`` 從不跨分片全局化。
    """

    chat_md5: str
    direction: str
    filter_key: str | None
    shard_ids: tuple[str, ...]
    account_epoch: str | int
    sort_seq: int
    shard_id: str
    local_id: int
    rowid: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": _CURSOR_VERSION,
            "chat_md5": self.chat_md5,
            "direction": self.direction,
            "filter": self.filter_key,
            "shard_ids": list(self.shard_ids),
            "account_epoch": self.account_epoch,
            "sort_seq": self.sort_seq,
            "shard_id": self.shard_id,
            "local_id": self.local_id,
            "rowid": self.rowid,
        }

    @classmethod
    def from_value(cls, value: Any) -> "MessageCursor":
        if not isinstance(value, Mapping):
            raise InvalidQuery("invalid_cursor")
        expected = {
            "version",
            "chat_md5",
            "direction",
            "filter",
            "shard_ids",
            "account_epoch",
            "sort_seq",
            "shard_id",
            "local_id",
            "rowid",
        }
        if (
            set(value) != expected
            or type(value.get("version")) is not int
            or value.get("version") != _CURSOR_VERSION
        ):
            raise InvalidQuery("invalid_cursor")
        chat_md5 = _validate_chat_md5(value.get("chat_md5"))
        direction = value.get("direction")
        if type(direction) is not str or direction not in ("asc", "desc"):
            raise InvalidQuery("invalid_cursor")
        filter_key = _validate_filter_key(value.get("filter"))
        raw_shard_ids = value.get("shard_ids")
        if type(raw_shard_ids) is not list:
            raise InvalidQuery("invalid_cursor")
        shard_ids = tuple(_validate_shard_id(item) for item in raw_shard_ids)
        if len(shard_ids) != len(set(shard_ids)) or tuple(sorted(shard_ids)) != shard_ids:
            raise InvalidQuery("invalid_cursor")
        account_epoch = _validate_account_epoch(value.get("account_epoch"))
        shard_id = _validate_shard_id(value.get("shard_id"))
        sort_seq = _strict_int(value.get("sort_seq"), "invalid_cursor")
        local_id = _strict_int(value.get("local_id"), "invalid_cursor")
        rowid = _strict_int(value.get("rowid"), "invalid_cursor")
        if shard_id not in shard_ids:
            raise InvalidQuery("invalid_cursor")
        return cls(
            chat_md5,
            direction,
            filter_key,
            shard_ids,
            account_epoch,
            sort_seq,
            shard_id,
            local_id,
            rowid,
        )


@dataclass(frozen=True, slots=True)
class IncrementalCursor:
    """Per-shard high-water marks for bounded incremental reads.

    v3 treats each high-water mark as the last consumed SQLite ``rowid`` in
    that shard's namespace.  Incremental output is therefore stable
    rowid-ascending merge order rather than a global ``sort_seq`` ordering;
    this is what prevents a bounded page from skipping a batch of late rows
    with lower sequence values.  ``shard_boundary`` mirrors the consumed
    witness for compatibility with the earlier shape.  ``gap_detected`` is
    deliberately carried forward so callers cannot mistake a changed/deleted
    witness for an exact-once continuation.
    """

    chat_md5: str
    filter_key: str | None
    shard_ids: tuple[str, ...]
    account_epoch: str | int
    shard_highwater: tuple[tuple[str, tuple[int, int, int] | None], ...]
    shard_boundary: tuple[tuple[str, tuple[int, int, int] | None], ...]
    gap_detected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": _INCREMENTAL_CURSOR_VERSION,
            "chat_md5": self.chat_md5,
            "direction": "asc",
            "filter": self.filter_key,
            "shard_ids": list(self.shard_ids),
            "account_epoch": self.account_epoch,
            "shard_highwater": [
                {
                    "shard_id": shard_id,
                    "sort_seq": None if boundary is None else boundary[0],
                    "local_id": None if boundary is None else boundary[1],
                    "rowid": None if boundary is None else boundary[2],
                }
                for shard_id, boundary in self.shard_highwater
            ],
            "shard_boundary": [
                {
                    "shard_id": shard_id,
                    "sort_seq": None if boundary is None else boundary[0],
                    "local_id": None if boundary is None else boundary[1],
                    "rowid": None if boundary is None else boundary[2],
                }
                for shard_id, boundary in self.shard_boundary
            ],
            "gap_detected": self.gap_detected,
        }

    @classmethod
    def from_value(cls, value: Any) -> "IncrementalCursor":
        if not isinstance(value, Mapping):
            raise InvalidQuery("invalid_cursor")
        expected = {
            "version",
            "chat_md5",
            "direction",
            "filter",
            "shard_ids",
            "account_epoch",
            "shard_highwater",
            "shard_boundary",
            "gap_detected",
        }
        if (
            set(value) != expected
            or type(value.get("version")) is not int
            or value.get("version") != _INCREMENTAL_CURSOR_VERSION
            or value.get("direction") != "asc"
            or type(value.get("gap_detected")) is not bool
        ):
            raise InvalidQuery("invalid_cursor")
        chat_md5 = _validate_chat_md5(value.get("chat_md5"))
        filter_key = _validate_filter_key(value.get("filter"))
        raw_shard_ids = value.get("shard_ids")
        if type(raw_shard_ids) is not list:
            raise InvalidQuery("invalid_cursor")
        shard_ids = tuple(_validate_shard_id(item) for item in raw_shard_ids)
        if len(shard_ids) != len(set(shard_ids)) or tuple(sorted(shard_ids)) != shard_ids:
            raise InvalidQuery("invalid_cursor")
        account_epoch = _validate_account_epoch(value.get("account_epoch"))
        def parse_positions(field: str) -> tuple[tuple[str, tuple[int, int, int] | None], ...]:
            raw_positions = value.get(field)
            if type(raw_positions) is not list or len(raw_positions) != len(shard_ids):
                raise InvalidQuery("invalid_cursor")
            positions: list[tuple[str, tuple[int, int, int] | None]] = []
            for expected_shard_id, raw_boundary in zip(shard_ids, raw_positions):
                if not isinstance(raw_boundary, Mapping) or set(raw_boundary) != {
                    "shard_id",
                    "sort_seq",
                    "local_id",
                    "rowid",
                }:
                    raise InvalidQuery("invalid_cursor")
                shard_id = _validate_shard_id(raw_boundary.get("shard_id"))
                if shard_id != expected_shard_id:
                    raise InvalidQuery("invalid_cursor")
                raw_values = (
                    raw_boundary.get("sort_seq"),
                    raw_boundary.get("local_id"),
                    raw_boundary.get("rowid"),
                )
                if all(value is None for value in raw_values):
                    boundary = None
                elif any(value is None for value in raw_values):
                    raise InvalidQuery("invalid_cursor")
                else:
                    boundary = tuple(
                        _strict_int(value, "invalid_cursor") for value in raw_values
                    )
                positions.append((shard_id, boundary))
            return tuple(positions)

        highwater = parse_positions("shard_highwater")
        shard_boundary = parse_positions("shard_boundary")
        return cls(
            chat_md5,
            filter_key,
            shard_ids,
            account_epoch,
            highwater,
            shard_boundary,
            value["gap_detected"],
        )


def _strict_int(value: Any, code: str) -> int:
    if type(value) is not int or not _MIN_SQL_INT <= value <= _MAX_SQL_INT:
        raise InvalidQuery(code)
    return value


def _validate_chat_md5(value: Any) -> str:
    if type(value) is not str or _MD5_RE.fullmatch(value) is None:
        raise InvalidQuery("invalid_chat_md5")
    return value


def _validate_shard_id(value: Any) -> str:
    if type(value) is not str or _SHARD_RE.fullmatch(value) is None:
        raise InvalidQuery("invalid_shard_id")
    return value


def _validate_account_epoch(value: Any) -> str | int:
    if type(value) is int:
        return _strict_int(value, "invalid_account_epoch")
    if (
        type(value) is str
        and 1 <= len(value) <= 256
        and "\x00" not in value
    ):
        return value
    raise InvalidQuery("invalid_account_epoch")


def _validate_filter_key(value: Any) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_KEYWORD_CHARS + 64
        or "\x00" in value
    ):
        raise InvalidQuery("invalid_cursor")
    return value


def _finite_float(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _json_size(value: Any) -> int:
    """Measure the public JSON form without allowing serialization to leak data."""

    try:
        return len(
            json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ReadStoreError("output_budget_exceeded") from exc


def _quote_identifier(value: str) -> str:
    """只給已通過固定 regex 的識別字加引號。"""

    return '"' + value.replace('"', '""') + '"'


def _schema_error() -> UnsupportedSchema:
    return UnsupportedSchema("unsupported")


class _QueryBudget:
    """一個查詢請求共用的 SQLite VM step 與 monotonic deadline。"""

    def __init__(self, max_steps: int, deadline: float):
        self.max_steps = max_steps
        self.deadline = deadline
        self.steps = 0
        self.exceeded = False

    def _progress(self) -> int:
        self.steps += _PROGRESS_GRANULARITY
        if self.steps > self.max_steps or time.monotonic() >= self.deadline:
            self.exceeded = True
            return 1
        return 0

    def _check(self) -> None:
        if time.monotonic() >= self.deadline:
            self.exceeded = True
            raise ReadStoreError("query_budget_exceeded")

    def fetchall(
        self, connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        return self._run(connection, sql, params, fetch_all=True)

    def fetchone(
        self, connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
    ) -> tuple[Any, ...] | None:
        return self._run(connection, sql, params, fetch_all=False)

    def _run(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[Any, ...],
        *,
        fetch_all: bool,
    ) -> Any:
        if time.monotonic() >= self.deadline:
            self.exceeded = True
            raise ReadStoreError("query_budget_exceeded")
        try:
            connection.set_progress_handler(self._progress, _PROGRESS_GRANULARITY)
            cursor = connection.execute(sql, params)
            result = cursor.fetchall() if fetch_all else cursor.fetchone()
            self._check()
            return result
        except ReadStoreError:
            raise
        except OverflowError:
            raise ReadStoreError("query_failed") from None
        except sqlite3.DatabaseError as exc:
            if self.exceeded or getattr(connection, 'persistent_budget_exhausted', False) is True:
                raise ReadStoreError("query_budget_exceeded") from None
            raise exc
        finally:
            connection.set_progress_handler(None, 0)

    def cursor(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> "_BudgetCursor":
        return _BudgetCursor(self, connection, sql, params)


class _BudgetCursor:
    """One-row-at-a-time SELECT cursor sharing a request budget."""

    def __init__(
        self,
        budget: _QueryBudget,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[Any, ...],
    ):
        self.budget = budget
        self.connection = connection
        self.cursor: sqlite3.Cursor | None = None
        self.closed = False
        try:
            budget._check()
            connection.set_progress_handler(budget._progress, _PROGRESS_GRANULARITY)
            self.cursor = connection.execute(sql, params)
        except ReadStoreError:
            self.close()
            raise
        except OverflowError:
            self.close()
            raise ReadStoreError("query_failed") from None
        except sqlite3.DatabaseError as exc:
            self.close()
            if budget.exceeded or getattr(connection, 'persistent_budget_exhausted', False) is True:
                raise ReadStoreError("query_budget_exceeded") from None
            raise exc

    def fetchone(self) -> tuple[Any, ...] | None:
        if self.closed or self.cursor is None:
            return None
        try:
            self.budget._check()
            row = self.cursor.fetchone()
            self.budget._check()
            return row
        except ReadStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            if (self.budget.exceeded
                    or getattr(self.connection, 'persistent_budget_exhausted', False) is True):
                raise ReadStoreError("query_budget_exceeded") from None
            raise exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.cursor is not None:
                self.cursor.close()
        finally:
            self.connection.set_progress_handler(None, 0)


def _columns(
    connection: sqlite3.Connection, table: str, budget: _QueryBudget
) -> set[str]:
    """取得固定表的欄位名；不改變 connection 的 row_factory。"""

    try:
        found = budget.fetchone(
            connection,
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if found is None:
            raise _schema_error()
        rows = budget.fetchall(
            connection, f"PRAGMA table_info({_quote_identifier(table)})"
        )
    except UnsupportedSchema:
        raise
    except ReadStoreError:
        raise
    except (sqlite3.DatabaseError, TypeError) as exc:
        raise _schema_error() from exc
    names = {row[1] for row in rows if len(row) > 1}
    return names


def _ensure_rowid(
    connection: sqlite3.Connection, table: str, budget: _QueryBudget
) -> None:
    """確認表可用 SQLite rowid 作同分片的最後穩定 tie-break。"""

    try:
        budget.fetchone(
            connection, f"SELECT rowid FROM {_quote_identifier(table)} LIMIT 0"
        )
    except (sqlite3.DatabaseError, TypeError) as exc:
        raise _schema_error() from exc


def _bound_content(
    value: Any, max_chars: int, *, truncated_hint: bool = False
) -> tuple[str | None, bool, bool]:
    """回傳 bounded content、是否完整可用、是否因長度被截斷。

    本核心不解壓 zstd/其他壓縮正文。不可安全解碼的 bytes 一律回傳
    ``content_available=False``，不猜測正文。
    """

    if isinstance(value, str):
        if len(value) <= max_chars and not truncated_hint:
            return value, True, False
        return value[:max_chars], False, True
    if isinstance(value, (bytes, bytearray, memoryview)):
        max_bytes = max_chars * 4
        if isinstance(value, memoryview):
            raw = value[:max_bytes].tobytes()
        else:
            raw = bytes(value[:max_bytes])
        if raw.startswith(b"\x28\xb5\x2f\xfd"):
            return None, False, truncated_hint
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, False, truncated_hint
        if len(text) > max_chars or truncated_hint:
            return text[:max_chars], False, True
        return text, True, False
    if value is None:
        return None, False, False
    return None, False, False


def _bound_username(value: Any, *, truncated_hint: bool = False) -> str:
    """Decode a bounded username without silently changing its identity."""

    if isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ReadStoreError("username_unavailable") from None
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
    else:
        raise ReadStoreError("username_unavailable")
    if truncated_hint or len(raw) > _MAX_USERNAME_BYTES:
        raise ReadStoreError("username_too_large")
    try:
        username = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadStoreError("username_unavailable") from None
    if not username or "\x00" in username:
        raise ReadStoreError("username_unavailable")
    return username


def _escape_like_literal(keyword: str) -> str:
    """建立 SQLite LIKE 的 literal pattern；值仍以 parameter 傳入。"""

    escaped = keyword.replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


class ReadStoreQuery:
    """在已開啟 SQLite connections 上執行有界、只讀的查詢。

    ``session_connection`` 通常是 ``session.db`` connection；
    ``message_connections`` 是 ``{shard_id: connection}``，每個 identity
    由 root 在開啟分片時提供。呼叫方仍擁有並負責關閉這些 connection。
    """

    def __init__(
        self,
        session_connection: sqlite3.Connection | None = None,
        message_connections: Mapping[str, sqlite3.Connection] | None = None,
        *,
        account_epoch: str | int | None = None,
        max_limit: int = MAX_LIMIT,
        max_text_chars: int = MAX_TEXT_CHARS,
        max_sql_steps: int = MAX_SQL_STEPS,
        query_timeout_seconds: int | float = 1.0,
    ):
        if type(max_limit) is not int or not 1 <= max_limit <= MAX_LIMIT:
            raise InvalidQuery("invalid_max_limit")
        if type(max_text_chars) is not int or not 1 <= max_text_chars <= MAX_TEXT_CHARS:
            raise InvalidQuery("invalid_max_text_chars")
        if type(max_sql_steps) is not int or not 1 <= max_sql_steps <= MAX_SQL_STEPS:
            raise InvalidQuery("invalid_max_sql_steps")
        timeout = _finite_float(query_timeout_seconds)
        if timeout is None or not 0 < timeout <= MAX_QUERY_SECONDS:
            raise InvalidQuery("invalid_query_timeout")
        if session_connection is not None and not isinstance(
            session_connection, sqlite3.Connection
        ):
            raise InvalidQuery("invalid_session_connection")
        if account_epoch is not None:
            account_epoch = _validate_account_epoch(account_epoch)
        self.session_connection = session_connection
        self.account_epoch = account_epoch
        self.max_limit = max_limit
        self.max_text_chars = max_text_chars
        self.max_text_bytes = min(max_text_chars * 4, MAX_TEXT_BYTES)
        self.max_sql_steps = max_sql_steps
        self.query_timeout_seconds = timeout
        self.message_shards = self._normalize_shards(message_connections)
        self.shard_ids = tuple(shard.shard_id for shard in self.message_shards)

    @staticmethod
    def _normalize_shards(value: Any) -> tuple[MessageShard, ...]:
        if value is None:
            return ()
        if not isinstance(value, Mapping):
            raise InvalidQuery("invalid_message_shards")
        pairs = list(value.items())
        if not 0 <= len(pairs) <= MAX_SHARDS:
            raise InvalidQuery("invalid_message_shards")
        output: list[MessageShard] = []
        seen: set[str] = set()
        for shard_id, connection in pairs:
            shard_id = _validate_shard_id(shard_id)
            if shard_id in seen or not isinstance(connection, sqlite3.Connection):
                raise InvalidQuery("invalid_message_shards")
            seen.add(shard_id)
            output.append(MessageShard(shard_id, connection))
        output.sort(key=lambda shard: shard.shard_id)
        return tuple(output)

    def _limit(self, value: Any) -> int:
        if type(value) is not int or not 1 <= value <= self.max_limit:
            raise InvalidQuery("invalid_limit")
        return value

    def _require_account_epoch(self) -> str | int:
        if self.account_epoch is None:
            raise InvalidQuery("account_epoch_required")
        return self.account_epoch

    def _budget(self, deadline: int | float | None) -> _QueryBudget:
        now = time.monotonic()
        if deadline is not None:
            deadline_value = _finite_float(deadline)
            if deadline_value is None:
                raise InvalidQuery("invalid_deadline")
            now_deadline = min(
                now + self.query_timeout_seconds, deadline_value
            )
        else:
            now_deadline = now + self.query_timeout_seconds
        if now_deadline <= now:
            raise ReadStoreError("query_budget_exceeded")
        return _QueryBudget(self.max_sql_steps, now_deadline)

    def _validate_cursor(
        self,
        cursor: Any,
        chat_md5: str,
        direction: str,
        filter_key: str | None,
    ) -> MessageCursor | None:
        if cursor is None:
            return None
        parsed = MessageCursor.from_value(cursor)
        if (
            parsed.chat_md5 != chat_md5
            or parsed.direction != direction
            or parsed.filter_key != filter_key
            or parsed.shard_ids != self.shard_ids
            or parsed.account_epoch != self._require_account_epoch()
        ):
            raise InvalidQuery("invalid_cursor")
        return parsed

    def get_sessions(
        self,
        limit: int = MAX_LIMIT,
        *,
        deadline: int | float | None = None,
    ) -> list[dict[str, Any]]:
        """讀 SessionTable 的 bounded rows，保留未讀/摘要/時間為獨立欄位。"""

        limit = self._limit(limit)
        if self.session_connection is None:
            raise _schema_error()
        budget = self._budget(deadline)
        columns = _columns(self.session_connection, _SESSION_TABLE, budget)
        if not _SESSION_COLUMNS <= columns:
            raise _schema_error()
        try:
            rows = budget.fetchall(
                self.session_connection,
                "SELECT substr(CAST(username AS BLOB), 1, ?) AS username, "
                "CASE WHEN length(CAST(username AS BLOB)) > ? "
                "THEN 1 ELSE 0 END AS username_truncated, "
                "unread_count, "
                "substr(CAST(summary AS BLOB), 1, ?) AS summary, "
                "CASE WHEN length(CAST(summary AS BLOB)) > ? "
                "THEN 1 ELSE 0 END AS summary_truncated, "
                "last_timestamp FROM SessionTable WHERE is_hidden=? "
                "ORDER BY sort_timestamp DESC, username ASC LIMIT ?",
                (
                    _MAX_USERNAME_BYTES,
                    _MAX_USERNAME_BYTES,
                    self.max_text_bytes,
                    self.max_text_bytes,
                    0,
                    limit,
                ),
            )
        except (sqlite3.DatabaseError, TypeError) as exc:
            raise _schema_error() from exc
        output: list[dict[str, Any]] = []
        for (
            username,
            username_truncated,
            unread_count,
            summary,
            summary_truncated,
            last_timestamp,
        ) in rows:
            username = _bound_username(
                username,
                truncated_hint=bool(username_truncated),
            )
            bounded_summary, summary_available, summary_truncated = _bound_content(
                summary,
                self.max_text_chars,
                truncated_hint=bool(summary_truncated),
            )
            if type(unread_count) is not int or unread_count < 0:
                unread_count = None
            output.append(
                {
                    "username": username,
                    "unread_count": unread_count,
                    "summary": bounded_summary,
                    "summary_available": summary_available,
                    "summary_truncated": summary_truncated,
                    "last_timestamp": last_timestamp,
                }
            )
        return output

    def _message_tables(
        self, chat_md5: str, budget: _QueryBudget, *, allow_empty: bool = False
    ) -> list[tuple[str, sqlite3.Connection, str, bool]]:
        table = "Msg_" + chat_md5
        found: list[tuple[str, sqlite3.Connection, str, bool]] = []
        for shard in self.message_shards:
            try:
                row = budget.fetchone(
                    shard.connection,
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
            except (sqlite3.DatabaseError, TypeError) as exc:
                raise _schema_error() from exc
            if row is None:
                continue
            columns = _columns(shard.connection, table, budget)
            if not _MESSAGE_COLUMNS <= columns:
                raise _schema_error()
            _ensure_rowid(shard.connection, table, budget)
            has_compression_flag = _MESSAGE_COMPRESSION_COLUMN in columns
            found.append((shard.shard_id, shard.connection, table, has_compression_flag))
        if not found and not allow_empty:
            # No message table cannot prove a complete-history query is empty.
            # Only start-from-now cursors may use this as an empty baseline.
            raise _schema_error()
        return found

    @staticmethod
    def _cursor_predicate(
        cursor: MessageCursor, shard_id: str
    ) -> tuple[str, tuple[Any, ...]]:
        if cursor.direction == "asc":
            if shard_id < cursor.shard_id:
                return "sort_seq > ?", (cursor.sort_seq,)
            if shard_id > cursor.shard_id:
                return "(sort_seq > ? OR sort_seq = ?)", (
                    cursor.sort_seq,
                    cursor.sort_seq,
                )
            return (
                "(sort_seq > ? OR (sort_seq = ? AND "
                "(local_id > ? OR (local_id = ? AND rowid > ?))))",
                (
                    cursor.sort_seq,
                    cursor.sort_seq,
                    cursor.local_id,
                    cursor.local_id,
                    cursor.rowid,
                ),
            )
        if shard_id < cursor.shard_id:
            return "sort_seq < ?", (cursor.sort_seq,)
        if shard_id > cursor.shard_id:
            return "(sort_seq < ? OR sort_seq = ?)", (
                cursor.sort_seq,
                cursor.sort_seq,
            )
        return (
            "(sort_seq < ? OR (sort_seq = ? AND "
            "(local_id > ? OR (local_id = ? AND rowid > ?))))",
            (
                cursor.sort_seq,
                cursor.sort_seq,
                cursor.local_id,
                cursor.local_id,
                cursor.rowid,
            ),
            )

    @staticmethod
    def _incremental_boundary_predicate(
        boundary: tuple[int, int, int],
    ) -> tuple[str, tuple[Any, ...]]:
        sort_seq, local_id, rowid = boundary
        return (
            "(sort_seq > ? OR (sort_seq = ? AND "
            "(local_id > ? OR (local_id = ? AND rowid > ?))))",
            (sort_seq, sort_seq, local_id, local_id, rowid),
        )

    @staticmethod
    def _message_item_key(item: Mapping[str, Any], direction: str) -> tuple[Any, ...]:
        if direction == "rowid":
            return (
                item["_rowid"],
                item["shard_id"],
                item["local_id"],
            )
        if direction == "asc":
            return (
                item["sort_seq"],
                item["shard_id"],
                item["local_id"],
                item["_rowid"],
            )
        return (
            -item["sort_seq"],
            item["shard_id"],
            item["local_id"],
            item["_rowid"],
        )

    def _merge_message_rows(
        self,
        chat_md5: str,
        shard_queries: Sequence[
            tuple[str, sqlite3.Connection, str, tuple[Any, ...]]
        ],
        *,
        direction: str,
        limit: int,
        budget: _QueryBudget,
        decode_bodies: bool = False,
    ) -> list[dict[str, Any]]:
        """Merge at most ``limit + 1`` converted rows with one row per shard.

        Each shard query is itself limited to ``limit + 1``.  The Python side
        retains only a heap head plus the bounded result page, rather than
        materializing every shard's candidate rows or their full bodies.
        """

        cursors: dict[str, _BudgetCursor] = {}
        heap: list[tuple[tuple[Any, ...], int, str, dict[str, Any]]] = []
        merged: list[dict[str, Any]] = []
        ordinal = 0
        try:
            for shard_id, connection, sql, params in shard_queries:
                cursor = budget.cursor(connection, sql, params)
                cursors[shard_id] = cursor
                row = cursor.fetchone()
                if row is None:
                    continue
                budget._check()
                item = self._message_item(
                    chat_md5,
                    shard_id,
                    row,
                    decode_bodies=decode_bodies,
                    deadline=budget.deadline,
                )
                heapq.heappush(
                    heap,
                    (
                        self._message_item_key(item, direction),
                        ordinal,
                        shard_id,
                        item,
                    ),
                )
                ordinal += 1
            while heap and len(merged) < limit + 1:
                _, _, shard_id, item = heapq.heappop(heap)
                merged.append(item)
                row = cursors[shard_id].fetchone()
                if row is None:
                    continue
                budget._check()
                next_item = self._message_item(
                    chat_md5,
                    shard_id,
                    row,
                    decode_bodies=decode_bodies,
                    deadline=budget.deadline,
                )
                heapq.heappush(
                    heap,
                    (
                        self._message_item_key(next_item, direction),
                        ordinal,
                        shard_id,
                        next_item,
                    ),
                )
                ordinal += 1
            return merged
        except ReadStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError) as exc:
            raise _schema_error() from exc
        finally:
            for cursor in cursors.values():
                cursor.close()

    def _page(
        self,
        chat_md5: str,
        *,
        direction: str,
        limit: int,
        cursor: Any = None,
        keyword: str | None = None,
        since_seq: int | None = None,
        filter_key: str | None = None,
        durable_continuation: bool = False,
        decode_bodies: bool = False,
        deadline: int | float | None = None,
    ) -> dict[str, Any]:
        chat_md5 = _validate_chat_md5(chat_md5)
        self._require_account_epoch()
        if direction not in ("asc", "desc"):
            raise InvalidQuery("invalid_direction")
        limit = self._limit(limit)
        parsed_cursor = self._validate_cursor(
            cursor, chat_md5, direction, filter_key
        )
        if parsed_cursor is not None and since_seq is not None:
            raise InvalidQuery("duplicate_cursor_boundary")
        if keyword is not None:
            if (
                type(keyword) is not str
                or len(keyword) > MAX_KEYWORD_CHARS
                or "\x00" in keyword
            ):
                raise InvalidQuery("invalid_keyword")
            pattern = _escape_like_literal(keyword)
        else:
            pattern = None
        if since_seq is not None:
            since_seq = _strict_int(since_seq, "invalid_since_seq")

        budget = self._budget(deadline)
        per_shard_limit = limit + 1
        shard_queries: list[
            tuple[str, sqlite3.Connection, str, tuple[Any, ...]]
        ] = []
        for shard_id, connection, table, has_compression_flag in self._message_tables(
            chat_md5, budget
        ):
            predicates: list[str] = []
            params: list[Any] = [self.max_text_bytes, self.max_text_bytes]
            if since_seq is not None:
                if direction == "asc":
                    predicates.append("sort_seq > ?")
                else:
                    predicates.append("sort_seq < ?")
                params.append(since_seq)
            if parsed_cursor is not None:
                predicate, predicate_params = self._cursor_predicate(
                    parsed_cursor, shard_id
                )
                predicates.append(predicate)
                params.extend(predicate_params)
            if pattern is not None:
                predicates.append(
                    "typeof(message_content)='text' AND "
                    "length(CAST(message_content AS BLOB)) <= ? AND "
                    "message_content LIKE ? ESCAPE '\\'"
                )
                params.extend((self.max_text_bytes, pattern))
            where = " WHERE " + " AND ".join(predicates) if predicates else ""
            order = (
                "ORDER BY sort_seq ASC, local_id ASC, rowid ASC"
                if direction == "asc"
                else "ORDER BY sort_seq DESC, local_id ASC, rowid ASC"
            )
            if decode_bodies:
                body_columns = (
                    "substr(CAST(message_content AS BLOB), 1, ?) AS message_content, "
                    "CASE WHEN length(CAST(message_content AS BLOB)) > ? "
                    "THEN 1 ELSE 0 END AS content_raw_truncated, "
                    "typeof(message_content) AS content_storage_type, "
                )
                if has_compression_flag:
                    body_columns += (
                        f"{_quote_identifier(_MESSAGE_COMPRESSION_COLUMN)} "
                        "AS content_compression_flag, "
                    )
                else:
                    body_columns += ""
                body_limit = _MAX_MESSAGE_RAW_BYTES
            else:
                body_columns = (
                    "substr(CAST(message_content AS BLOB), 1, ?) AS message_content, "
                    "CASE WHEN length(CAST(message_content AS BLOB)) > ? "
                    "THEN 1 ELSE 0 END AS content_truncated, "
                )
                body_limit = self.max_text_bytes
            tail = "server_id, sort_seq "
            sql = (
                "SELECT rowid, local_id, local_type, real_sender_id, "
                "create_time, "
                + body_columns
                + tail
                + f"FROM {_quote_identifier(table)}{where} {order} LIMIT ?"
            )
            params[0] = body_limit
            params[1] = body_limit
            params.append(per_shard_limit)
            shard_queries.append((shard_id, connection, sql, tuple(params)))

        items = self._merge_message_rows(
            chat_md5,
            shard_queries,
            direction=direction,
            limit=limit,
            budget=budget,
            decode_bodies=decode_bodies,
        )
        has_more = len(items) > limit
        items = items[:limit]
        next_cursor = None
        if items and (has_more or durable_continuation):
            last = items[-1]
            next_cursor = MessageCursor(
                chat_md5,
                direction,
                filter_key,
                self.shard_ids,
                self._require_account_epoch(),
                last["sort_seq"],
                last["shard_id"],
                last["local_id"],
                last["_rowid"],
            ).as_dict()
        elif durable_continuation and parsed_cursor is not None:
            next_cursor = parsed_cursor.as_dict()
        for item in items:
            item.pop("_rowid", None)
        if keyword is not None:
            coverage = "text_rows_full_content_only_bounded"
            unsupported_content = "blob_or_oversized_text_or_compressed"
        else:
            coverage = "bounded_message_table_query"
            unsupported_content = None
        result = {
            "items": items,
            "count": len(items),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "bounded": True,
            "full_history": False,
            "exact_once": False,
            "coverage": coverage,
            "unsupported_content": unsupported_content,
        }
        if keyword is not None:
            result["search_text_max_bytes"] = self.max_text_bytes
        return result

    def _message_item(
        self,
        chat_md5: str,
        shard_id: str,
        row: Sequence[Any],
        *,
        decode_bodies: bool = False,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        if decode_bodies:
            if len(row) not in (10, 11):
                raise _schema_error()
            (
                rowid,
                local_id,
                local_type,
                sender_id,
                create_time,
                content,
                raw_truncated,
                storage_type,
                *optional_flag,
                server_id,
                sort_seq,
            ) = row
            compression_flag = optional_flag[0] if optional_flag else None
        else:
            if len(row) != 9:
                raise _schema_error()
            (
                rowid,
                local_id,
                local_type,
                sender_id,
                create_time,
                content,
                content_truncated,
                server_id,
                sort_seq,
            ) = row
        for value in (rowid, local_id, sort_seq):
            if type(value) is not int:
                raise _schema_error()
        if decode_bodies:
            if type(raw_truncated) is not int or type(storage_type) is not str:
                raise _schema_error()
            if raw_truncated:
                bounded = None
                available = False
                truncated = True
                content_code = "raw_content_too_large"
                compression = "unknown"
                normalised_flag = (
                    compression_flag if type(compression_flag) is int else None
                )
                input_bytes = _MAX_MESSAGE_RAW_BYTES
                output_bytes = 0
            elif compression_flag is None and storage_type != "text":
                bounded = None
                available = False
                truncated = False
                content_code = "blob_without_compression_flag"
                compression = "unknown"
                normalised_flag = None
                input_bytes = len(content) if isinstance(content, (bytes, bytearray, memoryview)) else 0
                output_bytes = 0
            else:
                flag = compression_flag if compression_flag is not None else 0
                decoded = decode_body(
                    content,
                    flag,
                    deadline=deadline,
                    max_output_bytes=_MAX_MESSAGE_OUTPUT_BYTES,
                )
                content_code = decoded.code
                compression = decoded.compression
                normalised_flag = decoded.compression_flag
                input_bytes = decoded.input_bytes
                output_bytes = decoded.output_bytes
                if decoded.available and decoded.text is not None:
                    bounded = decoded.text[: self.max_text_chars]
                    truncated = len(decoded.text) > self.max_text_chars
                    # ``content_available`` means the returned text is the
                    # complete body.  A preview remains useful when it is
                    # truncated, but public projections reject the
                    # contradictory available+truncated combination.
                    available = not truncated
                else:
                    bounded = None
                    available = False
                    truncated = False
        else:
            if type(content_truncated) is not int:
                raise _schema_error()
            bounded, available, truncated = _bound_content(
                content,
                self.max_text_chars,
                truncated_hint=bool(content_truncated),
            )
            content_code = None
            compression = None
            normalised_flag = None
            input_bytes = None
            output_bytes = None
        identity = {
            "chat_md5": chat_md5,
            "shard_id": shard_id,
            "local_id": local_id,
            "server_id": server_id,
            "rowid": rowid,
        }
        item = {
            "shard_id": shard_id,
            "local_id": local_id,
            "local_type": local_type,
            "sender_id": sender_id,
            "create_time": create_time,
            "server_id": server_id,
            "sort_seq": sort_seq,
            "content": bounded,
            "content_available": available,
            "content_truncated": truncated,
            "message_identity": identity,
            "_rowid": rowid,
        }
        item.update(unavailable_sender_role())
        if decode_bodies:
            item.update(
                {
                    "content_code": content_code,
                    "content_compression": compression,
                    "content_compression_flag": normalised_flag,
                    "content_input_bytes": input_bytes,
                    "content_output_bytes": output_bytes,
                }
            )
        return item

    def get_messages(
        self,
        chat_md5: str,
        limit: int = 20,
        *,
        cursor: Any = None,
        direction: str = "desc",
        deadline: int | float | None = None,
    ) -> dict[str, Any]:
        """讀指定聊天最近/順序消息；使用複合游標避免同序號漏行。"""

        return self._page(
            chat_md5,
            direction=direction,
            limit=limit,
            cursor=cursor,
            filter_key=None,
            decode_bodies=True,
            deadline=deadline,
        )

    @staticmethod
    def _keyword_filter(keyword: Any) -> str:
        if (
            type(keyword) is not str
            or len(keyword) > MAX_KEYWORD_CHARS
            or "\x00" in keyword
        ):
            raise InvalidQuery("invalid_keyword")
        return "keyword:" + keyword

    def search_messages(
        self,
        chat_md5: str,
        keyword: str,
        limit: int = 20,
        *,
        cursor: Any = None,
        direction: str = "desc",
        deadline: int | float | None = None,
    ) -> dict[str, Any]:
        """只在 bounded、完整可供 SQLite 搜尋的 TEXT 正文中做 literal keyword 搜尋。

        BLOB、壓縮 BLOB 和超過 raw-byte 上限的 TEXT 不猜測成全文。
        """

        filter_key = self._keyword_filter(keyword)
        return self._page(
            chat_md5,
            direction=direction,
            limit=limit,
            cursor=cursor,
            keyword=keyword,
            filter_key=filter_key,
            deadline=deadline,
        )

    def _validate_incremental_cursor(
        self, cursor: Any, chat_md5: str
    ) -> IncrementalCursor | None:
        if cursor is None:
            return None
        parsed = IncrementalCursor.from_value(cursor)
        if (
            parsed.chat_md5 != chat_md5
            or parsed.shard_ids != self.shard_ids
            or parsed.account_epoch != self._require_account_epoch()
        ):
            raise InvalidQuery("invalid_cursor")
        return parsed

    @staticmethod
    def _since_filter_value(filter_key: str | None) -> int | None:
        if filter_key is None:
            return None
        prefix = "since_seq:"
        if not filter_key.startswith(prefix):
            raise InvalidQuery("invalid_cursor")
        suffix = filter_key[len(prefix) :]
        try:
            parsed = int(suffix)
        except (TypeError, ValueError):
            raise InvalidQuery("invalid_cursor") from None
        if str(parsed) != suffix:
            raise InvalidQuery("invalid_cursor")
        return _strict_int(parsed, "invalid_cursor")

    @staticmethod
    def _db_position(row: tuple[Any, ...] | None) -> tuple[int, int, int] | None:
        if row is None:
            return None
        if len(row) != 3:
            raise _schema_error()
        values = tuple(row)
        if any(
            type(value) is not int or not _MIN_SQL_INT <= value <= _MAX_SQL_INT
            for value in values
        ):
            raise _schema_error()
        return values  # type: ignore[return-value]

    def _incremental_page(
        self,
        chat_md5: str,
        *,
        limit: int,
        cursor: Any = None,
        since_seq: int | None = None,
        filter_key: str | None = None,
        bootstrap_now: bool = False,
        deadline: int | float | None = None,
    ) -> dict[str, Any]:
        chat_md5 = _validate_chat_md5(chat_md5)
        self._require_account_epoch()
        limit = self._limit(limit)
        parsed_cursor = self._validate_incremental_cursor(cursor, chat_md5)
        if parsed_cursor is not None and since_seq is not None:
            raise InvalidQuery("duplicate_cursor_boundary")
        if since_seq is not None:
            since_seq = _strict_int(since_seq, "invalid_since_seq")
        if parsed_cursor is None:
            base_since_seq = since_seq
            effective_filter = filter_key
        else:
            base_since_seq = (
                None
                if parsed_cursor.filter_key == "start_from:now"
                else self._since_filter_value(parsed_cursor.filter_key)
            )
            effective_filter = parsed_cursor.filter_key

        budget = self._budget(deadline)
        allow_empty = bootstrap_now or (
            parsed_cursor is not None
            and parsed_cursor.filter_key == "start_from:now"
        )
        tables = self._message_tables(chat_md5, budget, allow_empty=allow_empty)
        table_by_shard = {
            shard_id: (connection, table, has_compression_flag)
            for shard_id, connection, table, has_compression_flag in tables
        }
        prior_highwater = (
            dict(parsed_cursor.shard_highwater)
            if parsed_cursor is not None
            else {shard_id: None for shard_id in self.shard_ids}
        )
        gap_detected = bool(
            parsed_cursor is not None and parsed_cursor.gap_detected
        )
        current_witness: dict[str, tuple[int, int, int] | None] = {}

        for shard_id in self.shard_ids:
            table_info = table_by_shard.get(shard_id)
            if table_info is None:
                if parsed_cursor is not None and prior_highwater.get(shard_id) is not None:
                    gap_detected = True
                current_witness[shard_id] = None
                continue
            connection, table, _ = table_info
            try:
                witness_row = budget.fetchone(
                    connection,
                    f"SELECT sort_seq, local_id, rowid FROM {_quote_identifier(table)} "
                    "ORDER BY rowid DESC LIMIT 1",
                )
                current = self._db_position(witness_row)
                current_witness[shard_id] = current
                prior = prior_highwater.get(shard_id)
                if prior is not None:
                    found = budget.fetchone(
                        connection,
                        f"SELECT 1 FROM {_quote_identifier(table)} "
                        "WHERE rowid=? AND local_id=? AND sort_seq=? LIMIT 1",
                        (prior[2], prior[1], prior[0]),
                    )
                    if found is None:
                        gap_detected = True
                    if current is None or current[2] < prior[2]:
                        gap_detected = True
            except ReadStoreError:
                raise
            except (sqlite3.DatabaseError, TypeError) as exc:
                raise _schema_error() from exc

        if bootstrap_now:
            next_highwater = current_witness
            next_cursor = IncrementalCursor(
                chat_md5,
                effective_filter,
                self.shard_ids,
                self._require_account_epoch(),
                tuple(
                    (shard_id, next_highwater.get(shard_id))
                    for shard_id in self.shard_ids
                ),
                tuple(
                    (shard_id, next_highwater.get(shard_id))
                    for shard_id in self.shard_ids
                ),
                gap_detected,
            ).as_dict()
            return {
                "items": [],
                "count": 0,
                "has_more": False,
                "next_cursor": next_cursor,
                "bounded": True,
                "full_history": False,
                "exact_once": False,
                "gap_detected": gap_detected,
                "ordering": "rowid_asc_per_shard_merge",
                "coverage": "bounded_message_table_query",
                "unsupported_content": None,
            }

        shard_queries: list[
            tuple[str, sqlite3.Connection, str, tuple[Any, ...]]
        ] = []
        per_shard_limit = limit + 1
        for shard_id, connection, table, has_compression_flag in tables:
            predicates: list[str] = []
            predicate_params: list[Any] = []
            prior = prior_highwater.get(shard_id)
            if prior is not None:
                predicates.append("rowid > ?")
                predicate_params.append(prior[2])
            if base_since_seq is not None:
                predicates.append("sort_seq > ?")
                predicate_params.append(base_since_seq)
            where = " WHERE " + " AND ".join(predicates) if predicates else ""
            body_columns = (
                "substr(CAST(message_content AS BLOB), 1, ?) AS message_content, "
                "CASE WHEN length(CAST(message_content AS BLOB)) > ? "
                "THEN 1 ELSE 0 END AS content_raw_truncated, "
                "typeof(message_content) AS content_storage_type, "
            )
            if has_compression_flag:
                body_columns += (
                    f"{_quote_identifier(_MESSAGE_COMPRESSION_COLUMN)} "
                    "AS content_compression_flag, "
                )
            sql = (
                "SELECT rowid, local_id, local_type, real_sender_id, "
                "create_time, "
                + body_columns
                + "server_id, sort_seq "
                + f"FROM {_quote_identifier(table)}{where} "
                + "ORDER BY rowid ASC, local_id ASC LIMIT ?"
            )
            params = [_MAX_MESSAGE_RAW_BYTES, _MAX_MESSAGE_RAW_BYTES]
            params.extend(predicate_params)
            params.append(per_shard_limit)
            shard_queries.append((shard_id, connection, sql, tuple(params)))

        all_items = self._merge_message_rows(
            chat_md5,
            shard_queries,
            direction="rowid",
            limit=limit,
            budget=budget,
            decode_bodies=True,
        )

        # v3 is a rowid consumption cursor.  This intentionally gives up
        # sort_seq ordering for new reads so a bounded page cannot skip a
        # batch of late lower-sequence rows that was already scanned.  The
        # byte budget is applied before advancing that cursor: rows excluded
        # for output size remain eligible on the next call.
        def cursor_for(consumed: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            next_highwater = dict(prior_highwater)
            for item in consumed:
                candidate = (
                    item["sort_seq"],
                    item["local_id"],
                    item["_rowid"],
                )
                old = next_highwater.get(item["shard_id"])
                if old is None or candidate[2] > old[2]:
                    next_highwater[item["shard_id"]] = candidate
            return IncrementalCursor(
                chat_md5,
                effective_filter,
                self.shard_ids,
                self._require_account_epoch(),
                tuple(
                    (shard_id, next_highwater.get(shard_id))
                    for shard_id in self.shard_ids
                ),
                tuple(
                    (shard_id, next_highwater.get(shard_id))
                    for shard_id in self.shard_ids
                ),
                gap_detected,
            ).as_dict()

        def visible(item: Mapping[str, Any]) -> dict[str, Any]:
            result = dict(item)
            result.pop("_rowid", None)
            return result

        def result_for(
            items: list[dict[str, Any]],
            has_more: bool,
            next_cursor: dict[str, Any],
        ) -> dict[str, Any]:
            return {
                "items": items,
                "count": len(items),
                "has_more": has_more,
                "next_cursor": next_cursor,
                "bounded": True,
                "full_history": False,
                "exact_once": False,
                "gap_detected": gap_detected,
                "ordering": "rowid_asc_per_shard_merge",
                "coverage": "bounded_message_table_query",
                "unsupported_content": None,
            }

        candidate_limit = min(limit, len(all_items))
        selected: list[dict[str, Any]] | None = None
        selected_cursor: dict[str, Any] | None = None
        selected_has_more = False
        for count in range(1, candidate_limit + 1):
            raw_items = all_items[:count]
            candidate_items = [visible(item) for item in raw_items]
            candidate_has_more = count < len(all_items)
            candidate_cursor = cursor_for(raw_items)
            candidate_result = result_for(
                candidate_items, candidate_has_more, candidate_cursor
            )
            if _json_size(candidate_result) > MAX_OUTPUT_PAGE_BYTES:
                if count == 1:
                    raise ReadStoreError("row_too_large")
                break
            selected = candidate_items
            selected_cursor = candidate_cursor
            selected_has_more = candidate_has_more

        if selected is None:
            # An empty result has no row to trim.  A non-empty oversized page
            # was rejected above with a fixed error before its cursor moved.
            next_cursor = cursor_for(())
            result = result_for([], False, next_cursor)
            if _json_size(result) > MAX_OUTPUT_PAGE_BYTES:
                raise ReadStoreError("output_budget_exceeded")
            return result

        assert selected_cursor is not None
        result = result_for(selected, selected_has_more, selected_cursor)
        if _json_size(result) > MAX_OUTPUT_PAGE_BYTES:
            raise ReadStoreError("output_budget_exceeded")
        return result

    def get_new_messages(
        self,
        chat_md5: str,
        limit: int = MAX_LIMIT,
        *,
        cursor: Any = None,
        since_seq: int | None = None,
        start_from: str = "beginning",
        deadline: int | float | None = None,
    ) -> dict[str, Any]:
        """按 bounded rowid 游標讀增量；``since_seq`` 是可選下界。

        v3 cursor consumes each shard's SQLite rowid namespace.  This gives
        stable bounded continuation for late lower ``sort_seq`` inserts, while
        intentionally exposing insertion order rather than claiming a global
        sort_seq history.  ``start_from='now'`` anchors a cursor without
        returning existing rows.  ``start_from`` is considered only on that
        first cursorless call; continuation uses the cursor's bound filter.
        """

        if type(start_from) is not str or start_from not in ("beginning", "now"):
            raise InvalidQuery("invalid_start_from")
        if cursor is not None and since_seq is not None:
            raise InvalidQuery("duplicate_cursor_boundary")
        if start_from == "now" and cursor is None and since_seq is not None:
            raise InvalidQuery("start_from_now_with_since_seq")
        if since_seq is not None:
            since_seq = _strict_int(since_seq, "invalid_since_seq")
        if cursor is None:
            filter_key = "start_from:now" if start_from == "now" else (
                None if since_seq is None else f"since_seq:{since_seq}"
            )
        else:
            parsed = IncrementalCursor.from_value(cursor)
            # The cursor carries the bootstrap/filter identity.  The public
            # caller need not (and may not know to) repeat ``start_from`` on
            # continuation; any supplied spelling is intentionally ignored.
            if parsed.filter_key != "start_from:now":
                self._since_filter_value(parsed.filter_key)
            filter_key = parsed.filter_key
        return self._incremental_page(
            chat_md5,
            limit=limit,
            cursor=cursor,
            since_seq=since_seq,
            filter_key=filter_key,
            bootstrap_now=start_from == "now" and cursor is None,
            deadline=deadline,
        )

__all__ = [
    "InvalidQuery",
    "MAX_KEYWORD_CHARS",
    "MAX_LIMIT",
    "MAX_OUTPUT_PAGE_BYTES",
    "MAX_SHARDS",
    "MAX_SQL_STEPS",
    "MAX_TEXT_CHARS",
    "MAX_TEXT_BYTES",
    "IncrementalCursor",
    "MessageCursor",
    "MessageShard",
    "ReadStoreError",
    "ReadStoreQuery",
    "UnsupportedSchema",
]
