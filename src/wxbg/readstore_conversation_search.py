"""Bounded, read-only conversation and group search over the inbox reader.

Conversation search reuses :func:`readstore_inbox.read_inbox`. Group discovery
also reads bounded contact-only candidates, then all session rows (including
hidden ones). Neither source proves current membership. The search never
persists chat data and binds continuation to account, scope and keyword.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import sqlite3
import time
from typing import Any

from . import readstore_inbox as inbox
from .readstore_inbox import InboxReadError, read_inbox


MAX_LIMIT = inbox.MAX_LIMIT
MAX_KEYWORD_CHARS = 256
MAX_ACCOUNT_EPOCH_CHARS = 256
MAX_SCAN_ROWS = 500
MAX_RESULT_BYTES = inbox.MAX_RESULT_BYTES

_CURSOR_VERSION = 1
_INBOX_CURSOR_VERSION = 1
_GROUP_CURSOR_VERSION = 3
_GROUP_INBOX_CURSOR_VERSION = 3
_INBOX_ORDER = "sort_timestamp_desc_username_asc"
_GROUP_INBOX_ORDER = "username_asc"
_COVERAGE = "sessiontable_contact_bounded_nonhidden"
_MATCHING = "display_name_or_conversation_key_casefold_literal_substring"
_CURSOR_KEYS = frozenset({"version", "account_epoch", "keyword", "inner_cursor"})
_GROUP_CURSOR_BASE_KEYS = frozenset({"version", "scope", "account_epoch", "keyword", "phase"})
_GROUP_COVERAGE = "contact_only_then_sessiontable_bounded_group_candidates"
_GROUP_MATCHING = "chatroom_suffix_and_display_name_or_conversation_key_casefold_literal_substring"
_GROUP_SUFFIX = "@chatroom"


class ConversationSearchError(ValueError):
    """Fixed, machine-readable errors for the bounded search contract."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ConversationSearchError(code)


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


def _strict_keyword(value: Any) -> str:
    if (
        type(value) is str
        and 1 <= len(value) <= MAX_KEYWORD_CHARS
        and "\x00" not in value
    ):
        return value
    _fail("invalid_keyword")


def _strict_limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        _fail("invalid_limit")
    return value


def _effective_deadline(value: Any) -> float:
    if type(value) not in (int, float):
        _fail("invalid_deadline")
    try:
        deadline = float(value)
    except (OverflowError, ValueError):
        _fail("invalid_deadline")
    if not math.isfinite(deadline):
        _fail("invalid_deadline")
    now = time.monotonic()
    effective = min(deadline, now + inbox.MAX_QUERY_SECONDS)
    if effective <= now:
        _fail("query_budget_exceeded")
    return effective


def _validate_cursor(
    value: Any,
    *,
    account_epoch: str | int,
    keyword: str,
    scope: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    groups = scope == "groups"
    if not isinstance(value, Mapping):
        _fail("invalid_cursor")
    if groups:
        phase = value.get("phase")
        if phase == "contact":
            expected_keys = _GROUP_CURSOR_BASE_KEYS | {"after_username"}
        elif phase == "session":
            expected_keys = _GROUP_CURSOR_BASE_KEYS | {"inner_cursor"}
        else:
            _fail("invalid_cursor")
    else:
        expected_keys = _CURSOR_KEYS
    if set(value) != expected_keys:
        _fail("invalid_cursor")
    expected_version = _GROUP_CURSOR_VERSION if groups else _CURSOR_VERSION
    if type(value.get("version")) is not int or value["version"] != expected_version:
        _fail("invalid_cursor")
    if groups and value.get("scope") != "groups":
        _fail("invalid_cursor")
    try:
        cursor_epoch = _strict_account_epoch(value.get("account_epoch"))
    except ConversationSearchError:
        _fail("invalid_cursor")
    if cursor_epoch != account_epoch or value.get("keyword") != keyword:
        _fail("invalid_cursor")
    if groups and phase == "contact":
        after_username = value.get("after_username")
        if (
            type(after_username) is not str
            or not after_username.endswith(_GROUP_SUFFIX)
            or not 1 <= len(after_username) <= inbox.MAX_USERNAME_CHARS
            or "\x00" in after_username
        ):
            _fail("invalid_cursor")
        return {"phase": "contact", "after_username": after_username}
    inner = value.get("inner_cursor")
    if groups and inner is None:
        return {"phase": "session", "inner_cursor": None}
    if not isinstance(inner, Mapping):
        _fail("invalid_cursor")
    return {"phase": "session", "inner_cursor": dict(inner)} if groups else dict(inner)


def _inner_cursor_for_row(
    row: Mapping[str, Any], account_epoch: str | int, *, scope: str
) -> dict[str, Any]:
    username = row.get("conversation_key")
    sort_timestamp = row.get("sort_timestamp")
    if (
        type(username) is not str
        or not username
        or "\x00" in username
        or (sort_timestamp is not None and type(sort_timestamp) is not int)
    ):
        _fail("invalid_inbox_result")
    cursor = {
        "version": _GROUP_INBOX_CURSOR_VERSION if scope == "groups" else _INBOX_CURSOR_VERSION,
        "account_epoch": account_epoch,
        "unread_only": False,
        "order": _GROUP_INBOX_ORDER if scope == "groups" else _INBOX_ORDER,
        "sort_timestamp": None if scope == "groups" else sort_timestamp,
        "username": username,
    }
    if scope == "groups":
        cursor["include_hidden"] = True
    return cursor


def _outer_cursor(
    *,
    account_epoch: str | int,
    keyword: str,
    inner_cursor: Mapping[str, Any] | None,
    scope: str,
) -> dict[str, Any]:
    cursor = {
        "version": _GROUP_CURSOR_VERSION if scope == "groups" else _CURSOR_VERSION,
        "account_epoch": account_epoch,
        "keyword": keyword,
        "inner_cursor": dict(inner_cursor) if inner_cursor is not None else None,
    }
    if scope == "groups":
        cursor["scope"] = "groups"
        cursor["phase"] = "session"
    return cursor


def _contact_cursor(
    *, account_epoch: str | int, keyword: str, after_username: str
) -> dict[str, Any]:
    return {
        "version": _GROUP_CURSOR_VERSION,
        "scope": "groups",
        "account_epoch": account_epoch,
        "keyword": keyword,
        "phase": "contact",
        "after_username": after_username,
    }


def _matches(row: Mapping[str, Any], folded_keyword: str, *, scope: str) -> bool:
    display_name = row.get("display_name")
    conversation_key = row.get("conversation_key")
    if scope == "groups" and (
        type(conversation_key) is not str
        or not conversation_key.endswith(_GROUP_SUFFIX)
    ):
        return False
    return (
        type(display_name) is str and folded_keyword in display_name.casefold()
    ) or (
        type(conversation_key) is str
        and folded_keyword in conversation_key.casefold()
    )


def _json_size(value: Mapping[str, Any]) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
        )
    except (TypeError, ValueError, RecursionError):
        _fail("row_too_large")


def _make_result(
    *,
    items: list[dict[str, Any]],
    scanned: int,
    has_more: bool,
    next_cursor: dict[str, Any] | None,
    scan_limit_reached: bool,
    scope: str,
) -> dict[str, Any]:
    result = {
        "items": items,
        "count": len(items),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "scanned": scanned,
        "scan_limit_reached": scan_limit_reached,
        "coverage": _GROUP_COVERAGE if scope == "groups" else _COVERAGE,
        "matching": _GROUP_MATCHING if scope == "groups" else _MATCHING,
    }
    if scope == "groups":
        result["membership_unverified"] = True
    if _json_size(result) > MAX_RESULT_BYTES:
        _fail("row_too_large")
    return result


def _next_inner_cursor(page: Mapping[str, Any]) -> dict[str, Any]:
    value = page.get("next_cursor")
    if not isinstance(value, Mapping):
        _fail("invalid_inbox_result")
    return dict(value)


def _search_contact_groups(
    session: sqlite3.Connection,
    contact: sqlite3.Connection,
    *,
    account_epoch: str | int,
    keyword: str,
    folded_keyword: str,
    limit: int,
    after_username: str | None,
    deadline: float,
) -> dict[str, Any]:
    try:
        return _search_contact_groups_inner(
            session, contact, account_epoch=account_epoch, keyword=keyword,
            folded_keyword=folded_keyword, limit=limit,
            after_username=after_username, deadline=deadline,
        )
    except InboxReadError as error:
        raise ConversationSearchError(error.code) from None


def _search_contact_groups_inner(
    session: sqlite3.Connection,
    contact: sqlite3.Connection,
    *,
    account_epoch: str | int,
    keyword: str,
    folded_keyword: str,
    limit: int,
    after_username: str | None,
    deadline: float,
) -> dict[str, Any]:
    """Scan contact-only chatroom candidates before the session phase.

    Both connections are authenticated read-only snapshots from ``open_stores``.
    The same VM-step/deadline guard and text bounds as ``read_inbox`` apply.
    Contact entries are excluded if their exact key is already in SessionTable,
    so a stable traversal does not return that key twice.
    """

    budget = inbox._QueryBudget(deadline)
    if not inbox._CONTACT_COLUMNS <= inbox._table_columns(contact, inbox._CONTACT_TABLE, budget):
        _fail("unsupported_schema")
    if not inbox._SESSION_COLUMNS <= inbox._table_columns(session, inbox._SESSION_TABLE, budget):
        _fail("unsupported_schema")

    session_start = _outer_cursor(
        account_epoch=account_epoch, keyword=keyword, inner_cursor=None, scope="groups"
    )
    items: list[dict[str, Any]] = []
    scanned = 0
    last_cursor: dict[str, Any] | None = (
        _contact_cursor(account_epoch=account_epoch, keyword=keyword,
                        after_username=after_username)
        if after_username is not None else None
    )
    while scanned < MAX_SCAN_ROWS:
        page_limit = min(MAX_LIMIT, MAX_SCAN_ROWS - scanned)
        where = "typeof(c.username)='text' AND substr(c.username, -9)=?"
        params: list[Any] = [
            inbox.MAX_USERNAME_BYTES, inbox.MAX_USERNAME_BYTES,
            inbox.MAX_TEXT_BYTES, inbox.MAX_TEXT_BYTES,
            inbox.MAX_TEXT_BYTES, inbox.MAX_TEXT_BYTES,
            _GROUP_SUFFIX,
        ]
        if after_username is not None:
            where += " AND c.username COLLATE BINARY > ?"
            params.append(after_username)
        params.append(page_limit + 1)
        sql = (
            "SELECT "
            "substr(CAST(c.username AS BLOB), 1, ?), "
            "CASE WHEN length(CAST(c.username AS BLOB)) > ? THEN 1 ELSE 0 END, "
            "substr(CAST(c.remark AS BLOB), 1, ?), "
            "CASE WHEN length(CAST(c.remark AS BLOB)) > ? THEN 1 ELSE 0 END, "
            "substr(CAST(c.nick_name AS BLOB), 1, ?), "
            "CASE WHEN length(CAST(c.nick_name AS BLOB)) > ? THEN 1 ELSE 0 END "
            f"FROM {inbox._quote_identifier(inbox._CONTACT_TABLE)} AS c "
            f"WHERE {where} ORDER BY c.username COLLATE BINARY ASC LIMIT ?"
        )
        try:
            raw_rows = budget.fetchall(contact, sql, tuple(params))
            budget.check()
        except InboxReadError as error:
            raise ConversationSearchError(error.code) from None
        if not raw_rows:
            if scanned == 0:
                return search_conversations(
                    session, contact, account_epoch=account_epoch, keyword=keyword,
                    limit=limit, cursor=session_start, deadline=deadline,
                    scope="groups",
                )
            return _make_result(
                items=items, scanned=scanned, has_more=True,
                next_cursor=session_start, scan_limit_reached=False,
                scope="groups",
            )

        parsed: list[tuple[str, str, str, bool, bool]] = []
        seen: set[str] = set()
        for raw in raw_rows:
            budget.check()
            if len(raw) != 6 or any(type(flag) is not int or flag not in (0, 1)
                                    for flag in (raw[1], raw[3], raw[5])):
                _fail("unsupported_schema")
            username, available, truncated = inbox._bounded_text(
                raw[0], max_chars=inbox.MAX_USERNAME_CHARS,
                max_bytes=inbox.MAX_USERNAME_BYTES, truncated_hint=bool(raw[1]),
            )
            if (username is None or not username.endswith(_GROUP_SUFFIX)
                    or "\x00" in username or not available or truncated):
                _fail("row_too_large" if raw[1] else "unsupported_schema")
            if username in seen:
                _fail("ambiguous_contact_username")
            seen.add(username)
            remark, remark_available, remark_truncated = inbox._bounded_text(
                raw[2], max_chars=inbox.MAX_TEXT_CHARS,
                max_bytes=inbox.MAX_TEXT_BYTES, truncated_hint=bool(raw[3]),
            )
            nick_name, nick_available, nick_truncated = inbox._bounded_text(
                raw[4], max_chars=inbox.MAX_TEXT_CHARS,
                max_bytes=inbox.MAX_TEXT_BYTES, truncated_hint=bool(raw[5]),
            )
            display_name, display_source, display_available, display_truncated = (
                inbox._resolve_display(username, (
                    remark, remark_available and not remark_truncated, remark_truncated,
                    nick_name, nick_available and not nick_truncated, nick_truncated,
                ))
            )
            parsed.append((username, display_name, display_source,
                           display_available, display_truncated))

        page = parsed[:page_limit]
        usernames = [row[0] for row in page]
        placeholders = ", ".join("?" for _ in usernames)
        try:
            session_rows = budget.fetchall(
                session,
                f"SELECT s.username FROM {inbox._quote_identifier(inbox._SESSION_TABLE)} AS s "
                "WHERE typeof(s.username)='text' AND s.username COLLATE BINARY "
                f"IN ({placeholders}) LIMIT ?",
                tuple(usernames + [len(usernames) + 1]),
            )
            budget.check()
        except InboxReadError as error:
            raise ConversationSearchError(error.code) from None
        if any(len(row) != 1 or type(row[0]) is not str or row[0] not in seen
               for row in session_rows):
            _fail("unsupported_schema")
        session_keys = {row[0] for row in session_rows}
        page_has_more = len(parsed) > len(page)
        for index, (username, display_name, display_source,
                    display_available, display_truncated) in enumerate(page):
            budget.check()
            row_cursor = _contact_cursor(
                account_epoch=account_epoch, keyword=keyword,
                after_username=username,
            )
            next_cursor = (
                row_cursor if index + 1 < len(page) or page_has_more
                else session_start
            )
            if username not in session_keys:
                candidate_row = {
                    "conversation_key": username,
                    "display_name": display_name,
                    "display_source": display_source,
                    "display_available": display_available,
                    "display_truncated": display_truncated,
                    "unread_count": None,
                    "unread_known": False,
                    "summary": None,
                    "summary_available": False,
                    "summary_truncated": False,
                    "last_timestamp": None,
                    "sort_timestamp": None,
                    "is_hidden": None,
                    "candidate_source": "contact",
                    "membership_unverified": True,
                }
                if _matches(candidate_row, folded_keyword, scope="groups"):
                    try:
                        _make_result(
                            items=items + [candidate_row], scanned=scanned + 1,
                            has_more=True, next_cursor=next_cursor,
                            scan_limit_reached=scanned + 1 >= MAX_SCAN_ROWS,
                            scope="groups",
                        )
                    except ConversationSearchError as error:
                        if error.code != "row_too_large" or scanned == 0:
                            raise
                        return _make_result(
                            items=items, scanned=scanned, has_more=True,
                            next_cursor=last_cursor,
                            scan_limit_reached=False, scope="groups",
                        )
                    items.append(candidate_row)
            scanned += 1
            last_cursor = row_cursor
            after_username = username
            if len(items) >= limit or scanned >= MAX_SCAN_ROWS:
                return _make_result(
                    items=items, scanned=scanned, has_more=True,
                    next_cursor=next_cursor,
                    scan_limit_reached=scanned >= MAX_SCAN_ROWS,
                    scope="groups",
                )

        if not page_has_more:
            return _make_result(
                items=items, scanned=scanned, has_more=True,
                next_cursor=session_start, scan_limit_reached=False,
                scope="groups",
            )

    return _make_result(
        items=items, scanned=scanned, has_more=True,
        next_cursor=last_cursor, scan_limit_reached=True, scope="groups",
    )


def search_conversations(
    session: sqlite3.Connection,
    contact: sqlite3.Connection,
    *,
    account_epoch: str | int,
    keyword: str,
    limit: int = 20,
    cursor: Any = None,
    deadline: int | float,
    scope: str = "conversations",
) -> dict[str, Any]:
    """Search bounded session rows by display name or database key.

    Matching is a Unicode ``casefold`` literal substring operation.  The
    search scans at most :data:`MAX_SCAN_ROWS` rows per call and returns
    ``count`` for the items in this response only; it never claims a total
    match count.  ``next_cursor`` is wrapped with the exact account epoch,
    keyword, and the underlying inbox cursor. ``groups`` first scans contact
    entries absent from SessionTable, then all session rows, including hidden
    ones. Both sources only provide candidates whose database key ends in
    ``@chatroom``; that suffix does not establish current group membership.
    """

    if not isinstance(session, sqlite3.Connection):
        _fail("invalid_session_connection")
    if not isinstance(contact, sqlite3.Connection):
        _fail("invalid_contact_connection")
    account_epoch = _strict_account_epoch(account_epoch)
    keyword = _strict_keyword(keyword)
    limit = _strict_limit(limit)
    if type(scope) is not str or scope not in ("conversations", "groups"):
        _fail("invalid_scope")
    effective_deadline = _effective_deadline(deadline)
    resume = _validate_cursor(
        cursor, account_epoch=account_epoch, keyword=keyword, scope=scope
    )
    folded_keyword = keyword.casefold()
    if scope == "groups":
        if resume is None or resume["phase"] == "contact":
            return _search_contact_groups(
                session, contact, account_epoch=account_epoch, keyword=keyword,
                folded_keyword=folded_keyword, limit=limit,
                after_username=(resume["after_username"] if resume else None),
                deadline=effective_deadline,
            )
        inner_cursor = resume["inner_cursor"]
    else:
        inner_cursor = resume

    items: list[dict[str, Any]] = []
    scanned = 0
    last_inner_cursor = inner_cursor
    page_cursor = inner_cursor

    # Check the fixed metadata body before scanning any row.  This also keeps
    # a patched/smaller budget from producing an unbounded empty response.
    _make_result(
        items=items,
        scanned=0,
        has_more=False,
        next_cursor=None,
        scan_limit_reached=False,
        scope=scope,
    )

    while scanned < MAX_SCAN_ROWS:
        page_limit = min(MAX_LIMIT, MAX_SCAN_ROWS - scanned)
        try:
            page = read_inbox(
                session,
                contact,
                account_epoch=account_epoch,
                limit=page_limit,
                unread_only=False,
                include_hidden=scope == "groups",
                order=_GROUP_INBOX_ORDER if scope == "groups" else _INBOX_ORDER,
                cursor=page_cursor,
                deadline=effective_deadline,
            )
        except InboxReadError as error:
            raise ConversationSearchError(error.code) from None

        rows = page.get("conversations")
        if not isinstance(rows, list):
            _fail("invalid_inbox_result")
        page_has_more = page.get("has_more") is True

        if not rows:
            if page_has_more or page.get("next_cursor") is not None:
                _fail("invalid_inbox_result")
            return _make_result(
                items=items,
                scanned=scanned,
                has_more=False,
                next_cursor=None,
                scan_limit_reached=False,
                scope=scope,
            )

        for index, row in enumerate(rows):
            if scanned >= MAX_SCAN_ROWS:
                break
            if not isinstance(row, Mapping):
                _fail("invalid_inbox_result")
            row_cursor = _inner_cursor_for_row(row, account_epoch, scope=scope)
            row_has_more = index + 1 < len(rows) or page_has_more

            if _matches(row, folded_keyword, scope=scope):
                candidate_row = dict(row)
                if scope == "groups":
                    candidate_row["membership_unverified"] = True
                    candidate_row["candidate_source"] = "session"
                candidate_items = items + [candidate_row]
                candidate_result = {
                    "items": candidate_items,
                    "count": len(candidate_items),
                    "has_more": row_has_more,
                    "next_cursor": (
                        _outer_cursor(
                            account_epoch=account_epoch,
                            keyword=keyword,
                            inner_cursor=row_cursor,
                            scope=scope,
                        )
                        if row_has_more
                        else None
                    ),
                    "scanned": scanned + 1,
                    "scan_limit_reached": scanned + 1 >= MAX_SCAN_ROWS,
                    "coverage": _GROUP_COVERAGE if scope == "groups" else _COVERAGE,
                    "matching": _GROUP_MATCHING if scope == "groups" else _MATCHING,
                }
                if scope == "groups":
                    candidate_result["membership_unverified"] = True
                if _json_size(candidate_result) > MAX_RESULT_BYTES:
                    if scanned == 0:
                        _fail("row_too_large")
                    # Do not consume this row: the returned cursor remains at
                    # the last row actually evaluated before the oversized
                    # result candidate.
                    return _make_result(
                        items=items,
                        scanned=scanned,
                        has_more=True,
                        next_cursor=_outer_cursor(
                            account_epoch=account_epoch,
                            keyword=keyword,
                            inner_cursor=last_inner_cursor,
                            scope=scope,
                        ),
                        scan_limit_reached=False,
                        scope=scope,
                    )
                items.append(candidate_row)

            scanned += 1
            last_inner_cursor = row_cursor

            if len(items) >= limit:
                return _make_result(
                    items=items,
                    scanned=scanned,
                    has_more=row_has_more,
                    next_cursor=(
                        _outer_cursor(
                            account_epoch=account_epoch,
                            keyword=keyword,
                            inner_cursor=last_inner_cursor,
                            scope=scope,
                        )
                        if row_has_more
                        else None
                    ),
                    scan_limit_reached=False,
                    scope=scope,
                )

            if scanned >= MAX_SCAN_ROWS:
                return _make_result(
                    items=items,
                    scanned=scanned,
                    has_more=row_has_more,
                    next_cursor=(
                        _outer_cursor(
                            account_epoch=account_epoch,
                            keyword=keyword,
                            inner_cursor=last_inner_cursor,
                            scope=scope,
                        )
                        if row_has_more
                        else None
                    ),
                    scan_limit_reached=True,
                    scope=scope,
                )

        if not page_has_more:
            return _make_result(
                items=items,
                scanned=scanned,
                has_more=False,
                next_cursor=None,
                scan_limit_reached=False,
                scope=scope,
            )

        next_cursor = _next_inner_cursor(page)
        if page_cursor is not None and next_cursor == page_cursor:
            _fail("invalid_inbox_result")
        page_cursor = next_cursor

    # The loop returns as soon as the scan budget is consumed.  Keep a final
    # guard for defensive completeness if the reader ever supplies no rows.
    has_more = last_inner_cursor is not None
    return _make_result(
        items=items,
        scanned=scanned,
        has_more=has_more,
        next_cursor=(
            _outer_cursor(
                account_epoch=account_epoch,
                keyword=keyword,
                inner_cursor=last_inner_cursor,
                scope=scope,
            )
            if has_more
            else None
        ),
        scan_limit_reached=scanned >= MAX_SCAN_ROWS,
        scope=scope,
    )


__all__ = [
    "ConversationSearchError",
    "MAX_ACCOUNT_EPOCH_CHARS",
    "MAX_KEYWORD_CHARS",
    "MAX_LIMIT",
    "MAX_RESULT_BYTES",
    "MAX_SCAN_ROWS",
    "search_conversations",
]
