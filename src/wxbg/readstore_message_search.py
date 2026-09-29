"""Bounded decoded message search over caller-owned read-only SQLite shards.

The caller supplies authenticated, already-open connections.  This module
does not locate stores, open files, access keys, or use the WeChat client.  It
scans metadata with one heap head per shard, fetches at most one bounded body
at a time, and advances the public cursor only after that row is consumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import heapq
import json
import sqlite3
import time
from typing import Any

from .readstore_body import MAX_INPUT_BYTES, decode_body
from .sender_role import unavailable_sender_role
from .readstore_query import (
    InvalidQuery,
    MAX_LIMIT,
    MAX_SQL_STEPS,
    ReadStoreError,
    UnsupportedSchema,
    _BudgetCursor,
    _QueryBudget,
    _columns,
    _ensure_rowid,
    _finite_float,
    _quote_identifier,
    _schema_error,
    _strict_int,
    _validate_account_epoch,
    _validate_chat_md5,
    _validate_shard_id,
)


MAX_MESSAGE_SHARDS = 14
MAX_SCAN_ROWS = 500
MAX_TOTAL_INPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_PAGE_BYTES = 384 * 1024 - 256  # reserve the public account-epoch field
MAX_PREVIEW_CHARS = 4096
MAX_QUERY_SECONDS = 5.0
_MIN_SQL_INT = -(1 << 63)
_MAX_SQL_INT = (1 << 63) - 1
_MESSAGE_TABLE_PREFIX = "Msg_"
_FLAG_COLUMN = "WCDB_CT_message_content"
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
_SEARCH_COVERAGE = "bounded_decoded_message_search"
_SEARCH_MATCHING = "decoded_text_casefold_literal_substring"


@dataclass(frozen=True, slots=True)
class _Metadata:
    shard_id: str
    rowid: int
    local_id: int
    local_type: int | None
    sender_id: int | None
    create_time: int | None
    server_id: int | None
    sort_seq: int
    content_type: str
    content_bytes: int
    flag: int | None


@dataclass(slots=True)
class _ShardStream:
    shard_id: str
    connection: sqlite3.Connection
    table: str
    cursor: _BudgetCursor
    has_flag: bool


def _schema_int(value: Any, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or not _MIN_SQL_INT <= value <= _MAX_SQL_INT:
        raise _schema_error()
    return value


def _validate_connections(value: Any) -> tuple[tuple[str, sqlite3.Connection], ...]:
    if not isinstance(value, Mapping):
        raise InvalidQuery("invalid_message_shards")
    pairs = list(value.items())
    if not 1 <= len(pairs) <= MAX_MESSAGE_SHARDS:
        raise InvalidQuery("invalid_message_shards")
    output: list[tuple[str, sqlite3.Connection]] = []
    seen: set[str] = set()
    for raw_shard_id, connection in pairs:
        shard_id = _validate_shard_id(raw_shard_id)
        if shard_id in seen or not isinstance(connection, sqlite3.Connection):
            raise InvalidQuery("invalid_message_shards")
        seen.add(shard_id)
        output.append((shard_id, connection))
    output.sort(key=lambda pair: pair[0])
    return tuple(output)


def _validate_keyword(value: Any) -> str:
    if type(value) is not str or not 1 <= len(value) <= 256 or "\x00" in value:
        raise InvalidQuery("invalid_keyword")
    return value


def _validate_limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise InvalidQuery("invalid_limit")
    return value


def _make_budget(deadline: int | float | None) -> _QueryBudget:
    now = time.monotonic()
    if deadline is None:
        effective = now + MAX_QUERY_SECONDS
    else:
        deadline_value = _finite_float(deadline)
        if deadline_value is None:
            raise InvalidQuery("invalid_deadline")
        effective = min(now + MAX_QUERY_SECONDS, deadline_value)
    if effective <= now:
        raise ReadStoreError("query_budget_exceeded")
    return _QueryBudget(MAX_SQL_STEPS, effective)


def _validate_cursor(
    value: Any,
    *,
    account_epoch: str | int,
    chat_md5: str,
    keyword: str,
    shard_ids: tuple[str, ...],
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InvalidQuery("invalid_cursor")
    expected = {
        "version",
        "account_epoch",
        "chat_md5",
        "keyword",
        "direction",
        "shard_ids",
        "after",
    }
    if set(value) != expected or type(value.get("version")) is not int:
        raise InvalidQuery("invalid_cursor")
    if value.get("version") != 4 or value.get("direction") != "desc":
        raise InvalidQuery("invalid_cursor")
    try:
        cursor_epoch = _validate_account_epoch(value.get("account_epoch"))
        cursor_chat = _validate_chat_md5(value.get("chat_md5"))
        cursor_keyword = _validate_keyword(value.get("keyword"))
    except InvalidQuery:
        raise InvalidQuery("invalid_cursor") from None
    raw_shards = value.get("shard_ids")
    if type(raw_shards) is not list:
        raise InvalidQuery("invalid_cursor")
    try:
        parsed_shards = tuple(_validate_shard_id(item) for item in raw_shards)
    except InvalidQuery:
        raise InvalidQuery("invalid_cursor") from None
    if (
        parsed_shards != tuple(sorted(parsed_shards))
        or len(parsed_shards) != len(set(parsed_shards))
        or parsed_shards != shard_ids
    ):
        raise InvalidQuery("invalid_cursor")
    if (
        cursor_epoch != account_epoch
        or cursor_chat != chat_md5
        or cursor_keyword != keyword
    ):
        raise InvalidQuery("invalid_cursor")
    after = value.get("after")
    if not isinstance(after, Mapping) or set(after) != {
        "sort_seq",
        "shard_id",
        "local_id",
        "rowid",
    }:
        raise InvalidQuery("invalid_cursor")
    try:
        parsed_after = {
            "sort_seq": _strict_int(after.get("sort_seq"), "invalid_cursor"),
            "shard_id": _validate_shard_id(after.get("shard_id")),
            "local_id": _strict_int(after.get("local_id"), "invalid_cursor"),
            "rowid": _strict_int(after.get("rowid"), "invalid_cursor"),
        }
    except InvalidQuery:
        raise InvalidQuery("invalid_cursor") from None
    if parsed_after["shard_id"] not in shard_ids:
        raise InvalidQuery("invalid_cursor")
    return parsed_after


def _after_predicate(
    after: Mapping[str, Any] | None, shard_id: str
) -> tuple[str, tuple[Any, ...]]:
    if after is None:
        return "", ()
    sort_seq = after["sort_seq"]
    if shard_id < after["shard_id"]:
        return "sort_seq < ?", (sort_seq,)
    if shard_id > after["shard_id"]:
        return "(sort_seq < ? OR sort_seq = ?)", (sort_seq, sort_seq)
    return (
        "(sort_seq < ? OR (sort_seq = ? AND "
        "(local_id > ? OR (local_id = ? AND rowid > ?))))",
        (sort_seq, sort_seq, after["local_id"], after["local_id"], after["rowid"]),
    )


def _metadata_from_row(
    shard_id: str, row: tuple[Any, ...] | None, *, has_flag: bool
) -> _Metadata | None:
    if row is None:
        return None
    expected = 10 if has_flag else 9
    if len(row) != expected:
        raise _schema_error()
    (
        rowid,
        local_id,
        local_type,
        sender_id,
        create_time,
        server_id,
        sort_seq,
        content_type,
        content_bytes,
        *flag_values,
    ) = row
    rowid = _schema_int(rowid)
    local_id = _schema_int(local_id)
    sort_seq = _schema_int(sort_seq)
    local_type = _schema_int(local_type, nullable=True)
    sender_id = _schema_int(sender_id, nullable=True)
    create_time = _schema_int(create_time, nullable=True)
    server_id = _schema_int(server_id, nullable=True)
    if type(content_type) is not str:
        raise _schema_error()
    if content_bytes is None:
        content_bytes = 0
    content_bytes = _schema_int(content_bytes)
    if content_bytes < 0:
        raise _schema_error()
    flag = None
    if has_flag:
        flag = flag_values[0]
        if flag is not None:
            flag = _schema_int(flag)
    return _Metadata(
        shard_id,
        rowid,
        local_id,
        local_type,
        sender_id,
        create_time,
        server_id,
        sort_seq,
        content_type,
        content_bytes,
        flag,
    )


def _restore_progress(stream: _ShardStream, budget: _QueryBudget) -> None:
    # A body SELECT on the same connection clears sqlite's progress handler;
    # restore it before advancing the metadata cursor.
    stream.connection.set_progress_handler(budget._progress, 1)


def _next_metadata(stream: _ShardStream, budget: _QueryBudget) -> _Metadata | None:
    _restore_progress(stream, budget)
    row = stream.cursor.fetchone()
    return _metadata_from_row(stream.shard_id, row, has_flag=stream.has_flag)


def _open_streams(
    connections: tuple[tuple[str, sqlite3.Connection], ...],
    *,
    chat_md5: str,
    after: Mapping[str, Any] | None,
    budget: _QueryBudget,
) -> tuple[dict[str, _ShardStream], list[tuple[tuple[Any, ...], int, _Metadata]]]:
    table = _MESSAGE_TABLE_PREFIX + chat_md5
    streams: dict[str, _ShardStream] = {}
    heap: list[tuple[tuple[Any, ...], int, _Metadata]] = []
    ordinal = 0
    try:
        for shard_id, connection in connections:
            try:
                found = budget.fetchone(
                    connection,
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if found is None:
                    continue
                columns = _columns(connection, table, budget)
                if not _MESSAGE_COLUMNS <= columns:
                    raise _schema_error()
                _ensure_rowid(connection, table, budget)
                has_flag = _FLAG_COLUMN in columns
                selected = (
                    "rowid, local_id, local_type, real_sender_id, create_time, "
                    "server_id, sort_seq, typeof(message_content), "
                    "length(CAST(message_content AS BLOB))"
                )
                if has_flag:
                    selected += f", {_quote_identifier(_FLAG_COLUMN)}"
                predicate, params = _after_predicate(after, shard_id)
                where = f" WHERE {predicate}" if predicate else ""
                sql = (
                    f"SELECT {selected} FROM {_quote_identifier(table)}{where} "
                    "ORDER BY sort_seq DESC, local_id ASC, rowid ASC"
                )
                stream_cursor = budget.cursor(connection, sql, params)
                stream = _ShardStream(shard_id, connection, table, stream_cursor, has_flag)
                streams[shard_id] = stream
                metadata = _next_metadata(stream, budget)
                if metadata is not None:
                    heapq.heappush(
                        heap,
                        (
                            (-metadata.sort_seq, metadata.shard_id, metadata.local_id, metadata.rowid),
                            ordinal,
                            metadata,
                        ),
                    )
                    ordinal += 1
            except UnsupportedSchema:
                raise
            except ReadStoreError:
                raise
            except (sqlite3.DatabaseError, TypeError) as exc:
                raise _schema_error() from exc
        if not streams:
            raise _schema_error()
        return streams, heap
    except Exception:
        for stream in streams.values():
            stream.cursor.close()
        raise


def _fetch_body(
    stream: _ShardStream, metadata: _Metadata, budget: _QueryBudget
) -> tuple[str, bytes | None, int | None] | None:
    selected = "typeof(message_content), substr(CAST(message_content AS BLOB), 1, ?)"
    if stream.has_flag:
        selected += f", {_quote_identifier(_FLAG_COLUMN)}"
    try:
        row = budget.fetchone(
            stream.connection,
            f"SELECT {selected} FROM {_quote_identifier(stream.table)} WHERE rowid=?",
            (MAX_INPUT_BYTES + 1, metadata.rowid),
        )
    except (sqlite3.DatabaseError, TypeError) as exc:
        raise _schema_error() from exc
    if row is None:
        return None
    expected = 3 if stream.has_flag else 2
    if len(row) != expected or type(row[0]) is not str:
        raise _schema_error()
    body_type = row[0]
    body = row[1]
    if body is not None and not isinstance(body, (bytes, bytearray, memoryview)):
        raise _schema_error()
    flag = row[2] if stream.has_flag else None
    if flag is not None:
        flag = _schema_int(flag)
    return body_type, None if body is None else bytes(body), flag


def _cursor_value(
    *,
    account_epoch: str | int,
    chat_md5: str,
    keyword: str,
    shard_ids: tuple[str, ...],
    after: _Metadata | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(after, _Metadata):
        position = {
            "sort_seq": after.sort_seq,
            "shard_id": after.shard_id,
            "local_id": after.local_id,
            "rowid": after.rowid,
        }
    else:
        position = dict(after)
    return {
        "version": 4,
        "account_epoch": account_epoch,
        "chat_md5": chat_md5,
        "keyword": keyword,
        "direction": "desc",
        "shard_ids": list(shard_ids),
        "after": position,
    }


def _message_item(metadata: _Metadata, text: str, chat_md5: str) -> dict[str, Any]:
    truncated = len(text) > MAX_PREVIEW_CHARS
    item = {
        "shard_id": metadata.shard_id,
        "local_id": metadata.local_id,
        "local_type": metadata.local_type,
        "sender_id": metadata.sender_id,
        "create_time": metadata.create_time,
        "server_id": metadata.server_id,
        "sort_seq": metadata.sort_seq,
        "content": text[:MAX_PREVIEW_CHARS],
        "content_available": not truncated,
        "content_truncated": truncated,
        "content_status": "decoded_text",
        "message_identity": {
            "chat_md5": chat_md5,
            "shard_id": metadata.shard_id,
            "local_id": metadata.local_id,
            "server_id": metadata.server_id,
            "rowid": metadata.rowid,
        },
    }
    item.update(unavailable_sender_role())
    return item


def _result(
    *,
    items: list[dict[str, Any]],
    has_more: bool,
    next_cursor: dict[str, Any] | None,
    scanned: int,
    scan_limit_reached: bool,
    unsupported_count: int,
) -> dict[str, Any]:
    return {
        "items": items,
        "count": len(items),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "scanned": scanned,
        "scan_limit_reached": scan_limit_reached,
        "unsupported_count": unsupported_count,
        "coverage": _SEARCH_COVERAGE,
        "matching": _SEARCH_MATCHING,
        "bounded": True,
        "full_history": False,
        "exact_once": False,
    }


def _within_output_limit(result: dict[str, Any]) -> bool:
    try:
        size = len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        raise ReadStoreError("output_budget_exceeded") from None
    return size <= MAX_OUTPUT_PAGE_BYTES


def search_messages(
    message_connections: Mapping[str, sqlite3.Connection],
    *,
    account_epoch: str | int,
    chat_md5: str,
    keyword: str,
    limit: int = 20,
    cursor: Any = None,
    deadline: int | float | None = None,
) -> dict[str, Any]:
    """Search decoded text with bounded, descending continuation semantics."""

    connections = _validate_connections(message_connections)
    account_epoch = _validate_account_epoch(account_epoch)
    chat_md5 = _validate_chat_md5(chat_md5)
    keyword = _validate_keyword(keyword)
    limit = _validate_limit(limit)
    shard_ids = tuple(shard_id for shard_id, _ in connections)
    after = _validate_cursor(
        cursor,
        account_epoch=account_epoch,
        chat_md5=chat_md5,
        keyword=keyword,
        shard_ids=shard_ids,
    )
    keyword_folded = keyword.casefold()
    budget = _make_budget(deadline)
    streams: dict[str, _ShardStream] = {}
    heap: list[tuple[tuple[Any, ...], int, _Metadata]] = []
    items: list[dict[str, Any]] = []
    unsupported_count = 0
    scanned = 0
    input_bytes = 0
    last_consumed: _Metadata | None = None
    unknown_more = False
    try:
        streams, heap = _open_streams(
            connections,
            chat_md5=chat_md5,
            after=after,
            budget=budget,
        )
        while heap and scanned < MAX_SCAN_ROWS and len(items) < limit:
            _, _, metadata = heap[0]
            try:
                budget._check()
            except ReadStoreError:
                unknown_more = True
                break
            row_cost = min(metadata.content_bytes, MAX_INPUT_BYTES + 1)
            if input_bytes + row_cost > MAX_TOTAL_INPUT_BYTES:
                break
            try:
                body_row = _fetch_body(streams[metadata.shard_id], metadata, budget)
            except ReadStoreError:
                unknown_more = True
                break
            fetched_body_bytes = 0
            if body_row is not None and body_row[1] is not None:
                fetched_body_bytes = len(body_row[1])
            if input_bytes + fetched_body_bytes > MAX_TOTAL_INPUT_BYTES:
                # The metadata length can be stale if a caller mutates a
                # connection concurrently.  Keep this row unconsumed even
                # after the bounded fetch, so the public cursor remains
                # honest and the next call gets a fresh request budget.
                break
            input_bytes += fetched_body_bytes
            decoded_text: str | None = None
            candidate_unsupported = False
            if body_row is None:
                candidate_unsupported = True
            else:
                body_type, body, body_flag = body_row
                if body_type != metadata.content_type or body is None:
                    candidate_unsupported = True
                elif metadata.content_type == "text" and (
                    not streams[metadata.shard_id].has_flag
                    or body_flag in (None, 0)
                ):
                    decoded = decode_body(body, 0, deadline=budget.deadline)
                    if decoded.available:
                        decoded_text = decoded.text
                elif (
                    streams[metadata.shard_id].has_flag
                    and body_flag == 4
                    and metadata.content_type == "blob"
                ):
                    decoded = decode_body(body, 4, deadline=budget.deadline)
                    if decoded.available:
                        decoded_text = decoded.text
                else:
                    candidate_unsupported = True
            if time.monotonic() >= budget.deadline:
                unknown_more = True
                break
            if decoded_text is None:
                candidate_unsupported = True
            matched_item = None
            if decoded_text is not None and keyword_folded in decoded_text.casefold():
                matched_item = _message_item(metadata, decoded_text, chat_md5)
                candidate_cursor = _cursor_value(
                    account_epoch=account_epoch,
                    chat_md5=chat_md5,
                    keyword=keyword,
                    shard_ids=shard_ids,
                    after=metadata,
                )
                candidate_result = _result(
                    items=items + [matched_item],
                    has_more=True,
                    next_cursor=candidate_cursor,
                    scanned=scanned + 1,
                    scan_limit_reached=scanned + 1 >= MAX_SCAN_ROWS,
                    unsupported_count=unsupported_count,
                )
                if not _within_output_limit(candidate_result):
                    unknown_more = True
                    break
            heapq.heappop(heap)
            scanned += 1
            last_consumed = metadata
            if candidate_unsupported:
                unsupported_count += 1
            if matched_item is not None:
                items.append(matched_item)
            try:
                next_metadata = _next_metadata(streams[metadata.shard_id], budget)
            except ReadStoreError:
                unknown_more = True
                break
            if next_metadata is not None:
                ordinal = scanned + len(heap)
                heapq.heappush(
                    heap,
                    (
                        (
                            -next_metadata.sort_seq,
                            next_metadata.shard_id,
                            next_metadata.local_id,
                            next_metadata.rowid,
                        ),
                        ordinal,
                        next_metadata,
                    ),
                )
        scan_limit_reached = scanned >= MAX_SCAN_ROWS
        has_more = bool(heap) or unknown_more
        next_cursor = None
        if has_more:
            # A continuation with no consumed row cannot advance its cursor.
            # Report the exhausted request budget instead of an invalid page.
            if scanned == 0:
                raise ReadStoreError("query_budget_exceeded")
            if last_consumed is not None:
                next_cursor = _cursor_value(
                    account_epoch=account_epoch,
                    chat_md5=chat_md5,
                    keyword=keyword,
                    shard_ids=shard_ids,
                    after=last_consumed,
                )
            elif after is not None:
                next_cursor = _cursor_value(
                    account_epoch=account_epoch,
                    chat_md5=chat_md5,
                    keyword=keyword,
                    shard_ids=shard_ids,
                    after=after,
                )
            else:
                raise ReadStoreError("query_budget_exceeded")
        result = _result(
            items=items,
            has_more=has_more,
            next_cursor=next_cursor,
            scanned=scanned,
            scan_limit_reached=scan_limit_reached,
            unsupported_count=unsupported_count,
        )
        if not _within_output_limit(result):
            raise ReadStoreError("output_budget_exceeded")
        return result
    finally:
        for stream in streams.values():
            stream.cursor.close()


__all__ = [
    "InvalidQuery",
    "MAX_MESSAGE_SHARDS",
    "MAX_OUTPUT_PAGE_BYTES",
    "MAX_SCAN_ROWS",
    "MAX_TOTAL_INPUT_BYTES",
    "ReadStoreError",
    "UnsupportedSchema",
    "search_messages",
]
