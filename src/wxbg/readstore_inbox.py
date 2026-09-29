"""有界、只讀的 SessionTable Inbox 查詢核心。

呼叫方必須提供已開啟並已設好 authorizer 的 SQLite connections。本模組不開檔、
不取得 key、不解密、不建立 index，也不操作微信 UI。conversation_key 永遠是
資料庫 username；本模組不製造 session_ref 或其他 UI reference。
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import sqlite3
import time
from typing import Any


DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_TEXT_CHARS = 4096
MAX_TEXT_BYTES = MAX_TEXT_CHARS * 4
MAX_USERNAME_CHARS = 1024
MAX_USERNAME_BYTES = MAX_USERNAME_CHARS * 4
MAX_ACCOUNT_EPOCH_CHARS = 256
MAX_SQL_STEPS = 100_000
MAX_QUERY_SECONDS = 5.0
MAX_UNREAD_COUNT = (1 << 63) - 1
MAX_RESULT_BYTES = 384 * 1024

_CURSOR_VERSION = 1
_ALL_CURSOR_VERSION = 2
_GROUP_KEY_CURSOR_VERSION = 3
_ORDER = "sort_timestamp_desc_username_asc"
_GROUP_KEY_ORDER = "username_asc"
_SESSION_TABLE = "SessionTable"
_CONTACT_TABLE = "contact"
_SESSION_COLUMNS = frozenset(
    {
        "username",
        "unread_count",
        "summary",
        "last_timestamp",
        "sort_timestamp",
        "is_hidden",
    }
)
_CONTACT_COLUMNS = frozenset({"username", "remark", "nick_name"})


class InboxReadError(ValueError):
    """Inbox 查詢的固定、可供上游分類的錯誤。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise InboxReadError(code)


def _quote_identifier(value: str) -> str:
    # 呼叫方不可提供 identifier；這裡只引用模組內的固定表名。
    return '"' + value.replace('"', '""') + '"'


def _strict_account_epoch(value: Any) -> str | int:
    if type(value) is int:
        return value
    if (
        type(value) is str
        and 1 <= len(value) <= MAX_ACCOUNT_EPOCH_CHARS
        and "\x00" not in value
    ):
        return value
    _fail("invalid_account_epoch")


def _strict_limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        _fail("invalid_limit")
    return value


def _strict_deadline(value: Any) -> float:
    if type(value) not in (int, float):
        _fail("invalid_deadline")
    try:
        deadline = float(value)
    except (OverflowError, ValueError):
        _fail("invalid_deadline")
    if not math.isfinite(deadline):
        _fail("invalid_deadline")
    now = time.monotonic()
    effective = min(deadline, now + MAX_QUERY_SECONDS)
    if effective <= now:
        _fail("query_budget_exceeded")
    return effective


class _QueryBudget:
    """一個 read_inbox 呼叫共用的 VM step 與 monotonic deadline。"""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.steps = 0
        self.exceeded = False

    def _progress(self) -> int:
        self.steps += 1
        if self.steps > MAX_SQL_STEPS or time.monotonic() >= self.deadline:
            self.exceeded = True
            return 1
        return 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            self.exceeded = True
            _fail("query_budget_exceeded")

    def fetchall(
        self,
        connection: sqlite3.Connection,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        if time.monotonic() >= self.deadline:
            self.exceeded = True
            _fail("query_budget_exceeded")
        try:
            connection.set_progress_handler(self._progress, 1)
            cursor = connection.execute(sql, params)
            rows = cursor.fetchall()
            if time.monotonic() >= self.deadline:
                self.exceeded = True
                _fail("query_budget_exceeded")
            return rows
        except InboxReadError:
            raise
        except sqlite3.DatabaseError:
            if self.exceeded or getattr(connection, 'persistent_budget_exhausted', False) is True:
                _fail("query_budget_exceeded")
            _fail("query_failed")
        finally:
            try:
                connection.set_progress_handler(None, 0)
            except sqlite3.Error:
                # 原始查詢錯誤已經足夠固定；不要讓清理掩蓋它。
                pass


def _table_columns(
    connection: sqlite3.Connection, table: str, budget: _QueryBudget
) -> set[str]:
    try:
        exists = budget.fetchall(
            connection,
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if not exists:
            _fail("unsupported_schema")
        rows = budget.fetchall(
            connection, f"PRAGMA table_info({_quote_identifier(table)})"
        )
    except InboxReadError:
        raise
    except sqlite3.DatabaseError:
        _fail("unsupported_schema")
    names = {row[1] for row in rows if len(row) > 1 and type(row[1]) is str}
    return names


def _bounded_text(
    value: Any,
    *,
    max_chars: int,
    max_bytes: int,
    truncated_hint: bool,
) -> tuple[str | None, bool, bool]:
    """在 SQL raw-byte bound 後嚴格解碼，不猜測 opaque bytes。"""

    if value is None:
        return None, False, False
    if isinstance(value, memoryview):
        raw = value[:max_bytes].tobytes()
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value[:max_bytes])
    elif isinstance(value, str):
        try:
            raw = value.encode("utf-8", errors="strict")[:max_bytes]
        except UnicodeEncodeError:
            return None, False, truncated_hint
    else:
        return None, False, truncated_hint
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, False, truncated_hint
    if len(text) > max_chars or truncated_hint:
        return text[:max_chars], False, True
    return text, True, False


def _validate_cursor(
    value: Any,
    *,
    account_epoch: str | int,
    unread_only: bool,
    include_hidden: bool,
    order: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _fail("invalid_cursor")
    expected = {
        "version",
        "account_epoch",
        "unread_only",
        "order",
        "sort_timestamp",
        "username",
    }
    if include_hidden:
        expected.add("include_hidden")
    if set(value) != expected or type(value.get("version")) is not int:
        _fail("invalid_cursor")
    expected_version = (
        _GROUP_KEY_CURSOR_VERSION if order == _GROUP_KEY_ORDER
        else (_ALL_CURSOR_VERSION if include_hidden else _CURSOR_VERSION)
    )
    if value.get("version") != expected_version:
        _fail("invalid_cursor")
    if include_hidden and value.get("include_hidden") is not True:
        _fail("invalid_cursor")
    cursor_epoch = value.get("account_epoch")
    try:
        cursor_epoch = _strict_account_epoch(cursor_epoch)
    except InboxReadError:
        _fail("invalid_cursor")
    if cursor_epoch != account_epoch:
        _fail("invalid_cursor")
    if type(value.get("unread_only")) is not bool:
        _fail("invalid_cursor")
    if value.get("unread_only") is not unread_only:
        _fail("invalid_cursor")
    if value.get("order") != order:
        _fail("invalid_cursor")
    sort_timestamp = value.get("sort_timestamp")
    if order == _GROUP_KEY_ORDER and sort_timestamp is not None:
        _fail("invalid_cursor")
    if sort_timestamp is not None and (
        type(sort_timestamp) is not int
        or not -(1 << 63) <= sort_timestamp <= (1 << 63) - 1
    ):
        _fail("invalid_cursor")
    username = value.get("username")
    if (
        type(username) is not str
        or not username
        or len(username) > MAX_USERNAME_CHARS
        or "\x00" in username
    ):
        _fail("invalid_cursor")
    return {
        "sort_timestamp": sort_timestamp,
        "username": username,
    }


def _session_cursor_predicate(
    cursor: dict[str, Any], *, order: str
) -> tuple[str, tuple[Any, ...]]:
    timestamp = cursor["sort_timestamp"]
    username = cursor["username"]
    if order == _GROUP_KEY_ORDER:
        return "s.username COLLATE BINARY > ?", (username,)
    if timestamp is None:
        return "s.sort_timestamp IS NULL AND s.username > ?", (username,)
    return (
        "(s.sort_timestamp IS NULL OR s.sort_timestamp < ? OR "
        "(s.sort_timestamp = ? AND s.username > ?))",
        (timestamp, timestamp, username),
    )


def _cursor_for_row(
    row: Mapping[str, Any], *, account_epoch: str | int,
    unread_only: bool, include_hidden: bool, order: str,
) -> dict[str, Any]:
    cursor = {
        "version": (_GROUP_KEY_CURSOR_VERSION if order == _GROUP_KEY_ORDER
                    else (_ALL_CURSOR_VERSION if include_hidden else _CURSOR_VERSION)),
        "account_epoch": account_epoch,
        "unread_only": unread_only,
        "order": order,
        "sort_timestamp": None if order == _GROUP_KEY_ORDER else row["sort_timestamp"],
        "username": row["username"],
    }
    if include_hidden:
        cursor["include_hidden"] = True
    return cursor


def _normalize_unread(value: Any) -> tuple[int | None, bool]:
    if type(value) is int and 0 <= value <= MAX_UNREAD_COUNT:
        return value, True
    return None, False


def _normalize_hidden(value: Any) -> bool | None:
    if type(value) is int and value in (0, 1):
        return bool(value)
    return None


def _normalize_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not -(1 << 63) <= value <= (1 << 63) - 1:
        _fail("unsupported_schema")
    return value


def _resolve_display(
    username: str,
    contact: tuple[Any, ...] | None,
) -> tuple[str, str, bool, bool]:
    if contact is None:
        return username, "username", True, False
    remark, remark_available, remark_truncated, nick_name, nick_available, nick_truncated = contact
    if remark:
        return remark, "remark", remark_available, remark_truncated
    if nick_name:
        return nick_name, "nick_name", nick_available, nick_truncated
    return username, "username", True, False


def _json_size(value: dict[str, Any]) -> int:
    """以 worker 的 ensure_ascii JSON 形式估算可交付 body 大小。"""

    try:
        return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        _fail("row_too_large")


def read_inbox(
    session_connection: sqlite3.Connection,
    contact_connection: sqlite3.Connection,
    *,
    account_epoch: str | int,
    limit: int = DEFAULT_LIMIT,
    unread_only: bool = True,
    include_hidden: bool = False,
    order: str = _ORDER,
    cursor: Any = None,
    deadline: int | float,
) -> dict[str, Any]:
    """讀 bounded SessionTable conversations，不改變 read 狀態。

    回傳欄位固定為 ``conversations``、``next_cursor``、``has_more``、
    ``coverage``。預設排序為 ``sort_timestamp DESC, username ASC``；群聊搜尋
    內部可指定 ``username ASC``，避免新訊息改變分頁位置。cursor 綁定
    account epoch、unread filter、hidden 範圍與排序版本。預設模式只讀
    is_hidden=0；include_hidden 模式讀取全部 rows 並標示可識別的 0/1 狀態。
    summary、remark、nick_name
    都先在 SQL 以 raw bytes 截取，無法嚴格解碼時以 unavailable 表示。
    """

    if not isinstance(session_connection, sqlite3.Connection):
        _fail("invalid_session_connection")
    if not isinstance(contact_connection, sqlite3.Connection):
        _fail("invalid_contact_connection")
    account_epoch = _strict_account_epoch(account_epoch)
    limit = _strict_limit(limit)
    if type(unread_only) is not bool:
        _fail("invalid_unread_only")
    if type(include_hidden) is not bool:
        _fail("invalid_include_hidden")
    if type(order) is not str or order not in (_ORDER, _GROUP_KEY_ORDER) or (
        order == _GROUP_KEY_ORDER and (not include_hidden or unread_only)
    ):
        _fail("invalid_order")
    cursor_position = _validate_cursor(
        cursor, account_epoch=account_epoch, unread_only=unread_only,
        include_hidden=include_hidden, order=order,
    )
    budget = _QueryBudget(_strict_deadline(deadline))

    session_columns = _table_columns(session_connection, _SESSION_TABLE, budget)
    if not _SESSION_COLUMNS <= session_columns:
        _fail("unsupported_schema")
    contact_columns = _table_columns(contact_connection, _CONTACT_TABLE, budget)
    if not _CONTACT_COLUMNS <= contact_columns:
        _fail("unsupported_schema")

    predicates = [] if include_hidden else ["typeof(s.is_hidden)='integer' AND s.is_hidden=?"]
    params: list[Any] = [] if include_hidden else [0]
    if unread_only:
        predicates.append("typeof(s.unread_count)='integer' AND s.unread_count>?")
        params.append(0)
    if cursor_position is not None:
        predicate, cursor_params = _session_cursor_predicate(cursor_position, order=order)
        predicates.append(predicate)
        params.extend(cursor_params)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    session_sql = (
        "SELECT "
        "substr(CAST(s.username AS BLOB), 1, ?) AS username, "
        "CASE WHEN length(CAST(s.username AS BLOB)) > ? THEN 1 ELSE 0 END "
        "AS username_truncated, "
        "s.unread_count, "
        "substr(CAST(s.summary AS BLOB), 1, ?) AS summary, "
        "CASE WHEN length(CAST(s.summary AS BLOB)) > ? THEN 1 ELSE 0 END "
        "AS summary_truncated, "
        "s.last_timestamp, s.sort_timestamp, "
        "CASE WHEN typeof(s.is_hidden)='integer' AND s.is_hidden=0 THEN 0 "
        "WHEN typeof(s.is_hidden)='integer' AND s.is_hidden=1 THEN 1 "
        "ELSE NULL END AS is_hidden, "
        "typeof(s.username) AS username_type "
        f"FROM {_quote_identifier(_SESSION_TABLE)} AS s{where} "
        f"ORDER BY {'s.username COLLATE BINARY ASC' if order == _GROUP_KEY_ORDER else 's.sort_timestamp DESC, s.username ASC'} LIMIT ?"
    )
    session_params: list[Any] = [
        MAX_USERNAME_BYTES,
        MAX_USERNAME_BYTES,
        MAX_TEXT_BYTES,
        MAX_TEXT_BYTES,
    ]
    session_params.extend(params)
    session_params.append(limit + 1)
    session_rows = budget.fetchall(
        session_connection, session_sql, tuple(session_params)
    )
    budget.check()

    parsed_rows: list[dict[str, Any]] = []
    for row in session_rows:
        budget.check()
        if len(row) != 9:
            _fail("unsupported_schema")
        (
            username_raw,
            username_truncated,
            unread_raw,
            summary_raw,
            summary_truncated,
            last_timestamp,
            sort_timestamp,
            is_hidden,
            username_type,
        ) = row
        if username_type != "text":
            _fail("unsupported_schema")
        if (
            type(username_truncated) is not int
            or username_truncated not in (0, 1)
            or type(summary_truncated) is not int
            or summary_truncated not in (0, 1)
        ):
            _fail("unsupported_schema")
        username, username_available, username_was_truncated = _bounded_text(
            username_raw,
            max_chars=MAX_USERNAME_CHARS,
            max_bytes=MAX_USERNAME_BYTES,
            truncated_hint=bool(username_truncated),
        )
        if (
            username is None
            or not username
            or "\x00" in username
            or username_truncated
            or username_was_truncated
            or not username_available
        ):
            _fail("row_too_large" if username_truncated else "unsupported_schema")
        summary, summary_available, summary_was_truncated = _bounded_text(
            summary_raw,
            max_chars=MAX_TEXT_CHARS,
            max_bytes=MAX_TEXT_BYTES,
            truncated_hint=bool(summary_truncated),
        )
        parsed_rows.append(
            {
                "username": username,
                "unread_count": _normalize_unread(unread_raw),
                "summary": summary,
                "summary_available": summary_available,
                "summary_truncated": summary_was_truncated,
                "last_timestamp": _normalize_timestamp(last_timestamp),
                "sort_timestamp": _normalize_timestamp(sort_timestamp),
                "is_hidden": _normalize_hidden(is_hidden),
            }
        )

    page_rows = parsed_rows[:limit]
    contacts: dict[str, tuple[Any, ...]] = {}
    usernames = list(dict.fromkeys(row["username"] for row in parsed_rows))
    if usernames:
        placeholders = ", ".join("?" for _ in usernames)
        contact_sql = (
            "SELECT "
            "substr(CAST(c.username AS BLOB), 1, ?) AS username, "
            "CASE WHEN length(CAST(c.username AS BLOB)) > ? THEN 1 ELSE 0 END "
            "AS username_truncated, "
            "substr(CAST(c.remark AS BLOB), 1, ?) AS remark, "
            "CASE WHEN length(CAST(c.remark AS BLOB)) > ? THEN 1 ELSE 0 END "
            "AS remark_truncated, "
            "substr(CAST(c.nick_name AS BLOB), 1, ?) AS nick_name, "
            "CASE WHEN length(CAST(c.nick_name AS BLOB)) > ? THEN 1 ELSE 0 END "
            "AS nick_name_truncated "
            f"FROM {_quote_identifier(_CONTACT_TABLE)} AS c "
            f"WHERE c.username IN ({placeholders}) LIMIT ?"
        )
        contact_params: list[Any] = [
            MAX_USERNAME_BYTES,
            MAX_USERNAME_BYTES,
            MAX_TEXT_BYTES,
            MAX_TEXT_BYTES,
            MAX_TEXT_BYTES,
            MAX_TEXT_BYTES,
        ]
        contact_params.extend(usernames)
        contact_params.append(len(usernames) + 1)
        contact_rows = budget.fetchall(
            contact_connection, contact_sql, tuple(contact_params)
        )
        budget.check()
        for row in contact_rows:
            budget.check()
            if len(row) != 6:
                _fail("unsupported_schema")
            (
                username_raw,
                username_truncated,
                remark_raw,
                remark_truncated,
                nick_name_raw,
                nick_name_truncated,
            ) = row
            if any(
                type(flag) is not int or flag not in (0, 1)
                for flag in (username_truncated, remark_truncated, nick_name_truncated)
            ):
                _fail("unsupported_schema")
            username, username_available, username_was_truncated = _bounded_text(
                username_raw,
                max_chars=MAX_USERNAME_CHARS,
                max_bytes=MAX_USERNAME_BYTES,
                truncated_hint=bool(username_truncated),
            )
            if (
                username is None
                or username_truncated
                or username_was_truncated
                or not username_available
            ):
                _fail("row_too_large" if username_truncated else "unsupported_schema")
            remark, remark_available, remark_was_truncated = _bounded_text(
                remark_raw,
                max_chars=MAX_TEXT_CHARS,
                max_bytes=MAX_TEXT_BYTES,
                truncated_hint=bool(remark_truncated),
            )
            nick_name, nick_available, nick_was_truncated = _bounded_text(
                nick_name_raw,
                max_chars=MAX_TEXT_CHARS,
                max_bytes=MAX_TEXT_BYTES,
                truncated_hint=bool(nick_name_truncated),
            )
            if username in contacts:
                _fail("ambiguous_contact_username")
            contacts[username] = (
                remark,
                remark_available and not remark_was_truncated,
                remark_was_truncated,
                nick_name,
                nick_available and not nick_was_truncated,
                nick_was_truncated,
            )

    conversations: list[dict[str, Any]] = []
    for row in page_rows:
        budget.check()
        unread_count, unread_known = row["unread_count"]
        display_name, display_source, display_available, display_truncated = (
            _resolve_display(row["username"], contacts.get(row["username"]))
        )
        conversation = {
            "conversation_key": row["username"],
            "display_name": display_name,
            "display_source": display_source,
            "display_available": display_available,
            "display_truncated": display_truncated,
            "unread_count": unread_count,
            "unread_known": unread_known,
            "summary": row["summary"],
            "summary_available": row["summary_available"],
            "summary_truncated": row["summary_truncated"],
            "last_timestamp": row["last_timestamp"],
            "sort_timestamp": row["sort_timestamp"],
        }
        if include_hidden:
            conversation["is_hidden"] = row["is_hidden"]
        conversations.append(conversation)

    # Worker JSON 有整體上限；縮短同一排序前綴，讓下一頁由最後實際
    # 回傳的 row 續讀。先檢查每個 trial，避免回傳超出上游的 body。
    bounded_conversations: list[dict[str, Any]] = []
    for index, conversation in enumerate(conversations):
        budget.check()
        trial = bounded_conversations + [conversation]
        trial_has_more = len(parsed_rows) > len(trial)
        trial_cursor = None
        if trial_has_more:
            last = page_rows[index]
            trial_cursor = _cursor_for_row(last, account_epoch=account_epoch,
                                           unread_only=unread_only,
                                           include_hidden=include_hidden, order=order)
        trial_result = {
            "conversations": trial,
            "next_cursor": trial_cursor,
            "has_more": trial_has_more,
            "coverage": ("sessiontable_contact_bounded_all" if include_hidden
                         else "sessiontable_contact_bounded_nonhidden"),
        }
        if _json_size(trial_result) > MAX_RESULT_BYTES:
            if not bounded_conversations:
                _fail("row_too_large")
            break
        bounded_conversations.append(conversation)

    has_more = len(parsed_rows) > len(bounded_conversations)
    next_cursor = None
    if has_more and bounded_conversations:
        last = page_rows[len(bounded_conversations) - 1]
        next_cursor = _cursor_for_row(last, account_epoch=account_epoch,
                                      unread_only=unread_only,
                                      include_hidden=include_hidden, order=order)
    result = {
        "conversations": bounded_conversations,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "coverage": ("sessiontable_contact_bounded_all" if include_hidden
                     else "sessiontable_contact_bounded_nonhidden"),
    }
    budget.check()
    if _json_size(result) > MAX_RESULT_BYTES:
        _fail("row_too_large")
    return result


__all__ = [
    "DEFAULT_LIMIT",
    "InboxReadError",
    "MAX_LIMIT",
    "MAX_RESULT_BYTES",
    "MAX_SQL_STEPS",
    "MAX_TEXT_CHARS",
    "read_inbox",
]
