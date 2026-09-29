"""Bounded exact-row lookup for received-file declarations.

This module deliberately stops at the authenticated message row and its
bounded, decoded declaration.  It accepts only caller-owned SQLite
connections; it never opens a database, reads a cache or filesystem, obtains
keys, or returns message body bytes.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import importlib
import json
import math
import re
import sqlite3
import time
from typing import Any

from .readstore_body import MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, decode_body
from .readstore_image_locator import (
    MAX_PACKED_INFO_BYTES,
    ImageLocatorError,
    parse_image_locator,
)
from .readstore_query import (
    MAX_QUERY_SECONDS,
    MAX_SHARDS,
    MAX_SQL_STEPS,
    InvalidQuery,
    ReadStoreError,
    UnsupportedSchema,
    _QueryBudget,
    _columns,
    _quote_identifier,
    _strict_int,
    _validate_account_epoch,
    _validate_shard_id,
)


_MAX_CONVERSATION_CHARS = 1024
_MAX_CONVERSATION_BYTES = 4096
_MAX_DECLARATION_JSON_BYTES = 64 * 1024
_IDENTITY_KEYS = frozenset(
    ("chat_md5", "shard_id", "local_id", "server_id", "rowid")
)
_REQUIRED_COLUMNS = frozenset(
    ("local_id", "local_type", "create_time", "message_content", "server_id")
)
_COMPRESSION_COLUMN = "WCDB_CT_message_content"
_PACKED_INFO_COLUMN = "packed_info_data"
_MD5_RE = re.compile(r"[0-9a-f]{32}\Z")
_MIN_SQL_INT = -(1 << 63)
_MAX_SQL_INT = (1 << 63) - 1

# The public name is useful to callers that want a stable exception family;
# all errors still carry only a fixed ``code`` and never raw message content.
AttachmentQueryError = ReadStoreError


def _fail(code: str, *, invalid: bool = False) -> None:
    error_type = InvalidQuery if invalid else ReadStoreError
    raise error_type(code)


def _validate_deadline(value: Any) -> float:
    if type(value) not in (int, float):
        _fail("invalid_deadline", invalid=True)
    try:
        deadline = float(value)
    except (OverflowError, ValueError):
        _fail("invalid_deadline", invalid=True)
    if not math.isfinite(deadline):
        _fail("invalid_deadline", invalid=True)
    return deadline


def _normalise_connections(value: Any) -> dict[str, sqlite3.Connection]:
    if not isinstance(value, Mapping):
        _fail("invalid_message_connections", invalid=True)
    try:
        pairs = list(value.items())
    except Exception:
        _fail("invalid_message_connections", invalid=True)
    if not 1 <= len(pairs) <= MAX_SHARDS:
        _fail("invalid_message_connections", invalid=True)
    result: dict[str, sqlite3.Connection] = {}
    for raw_shard_id, connection in pairs:
        try:
            shard_id = _validate_shard_id(raw_shard_id)
        except ReadStoreError:
            _fail("invalid_message_connections", invalid=True)
        if shard_id in result or not isinstance(connection, sqlite3.Connection):
            _fail("invalid_message_connections", invalid=True)
        result[shard_id] = connection
    return result


def _validate_chat_md5(value: Any) -> str:
    if type(value) is not str or _MD5_RE.fullmatch(value) is None:
        _fail("invalid_message_identity", invalid=True)
    return value


def _validate_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _IDENTITY_KEYS:
        _fail("invalid_message_identity", invalid=True)
    chat_md5 = _validate_chat_md5(value.get("chat_md5"))
    try:
        shard_id = _validate_shard_id(value.get("shard_id"))
    except ReadStoreError:
        _fail("invalid_message_identity", invalid=True)
    local_id = _strict_int(value.get("local_id"), "invalid_message_identity")
    rowid = _strict_int(value.get("rowid"), "invalid_message_identity")
    server_id = value.get("server_id")
    if server_id is not None:
        server_id = _strict_int(server_id, "invalid_message_identity")
    return {
        "chat_md5": chat_md5,
        "shard_id": shard_id,
        "local_id": local_id,
        "server_id": server_id,
        "rowid": rowid,
    }


def _validate_conversation_key(value: Any) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= _MAX_CONVERSATION_CHARS
        or "\x00" in value
    ):
        _fail("invalid_conversation_key", invalid=True)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("invalid_conversation_key", invalid=True)
    if len(encoded) > _MAX_CONVERSATION_BYTES:
        _fail("invalid_conversation_key", invalid=True)
    return value


def _make_budget(deadline: Any) -> _QueryBudget:
    requested = _validate_deadline(deadline)
    now = time.monotonic()
    if requested <= now:
        _fail("query_budget_exceeded")
    # A caller-supplied future deadline cannot turn one request into an
    # unbounded scan.  The caller's earlier deadline remains authoritative.
    effective = min(requested, now + MAX_QUERY_SECONDS)
    return _QueryBudget(MAX_SQL_STEPS, effective)


def _table_and_schema(
    connection: sqlite3.Connection,
    chat_md5: str,
    budget: _QueryBudget,
) -> tuple[str, bool, bool]:
    table = "Msg_" + chat_md5
    try:
        columns = _columns(connection, table, budget)
        if not _REQUIRED_COLUMNS <= columns:
            raise UnsupportedSchema("unsupported")
        # WITHOUT ROWID tables cannot supply the identity namespace promised by
        # this API.  The identifier is generated from a strict MD5, never from
        # an arbitrary caller string.
        budget.fetchone(
            connection,
            f"SELECT rowid FROM {_quote_identifier(table)} LIMIT 0",
        )
    except (UnsupportedSchema, ReadStoreError):
        raise
    except (sqlite3.DatabaseError, TypeError):
        raise UnsupportedSchema("unsupported") from None
    return table, _COMPRESSION_COLUMN in columns, _PACKED_INFO_COLUMN in columns


def _read_exact_row(
    connection: sqlite3.Connection,
    table: str,
    identity: Mapping[str, Any],
    has_compression_flag: bool,
    has_packed_info: bool,
    budget: _QueryBudget,
) -> tuple[Any, ...]:
    body_columns = (
        "substr(CAST(message_content AS BLOB), 1, ?) AS raw_body, "
        "CASE WHEN length(CAST(message_content AS BLOB)) > ? "
        "THEN 1 ELSE 0 END AS raw_truncated, "
        "typeof(message_content) AS storage_type"
    )
    flag_column = ""
    if has_compression_flag:
        flag_column = (
            ", "
            + _quote_identifier(_COMPRESSION_COLUMN)
            + " AS compression_flag"
        )
    packed_columns = ""
    if has_packed_info:
        packed_columns = (
            ", substr(CAST("
            + _quote_identifier(_PACKED_INFO_COLUMN)
            + " AS BLOB), 1, ?) AS packed_info, "
            + "CASE WHEN length(CAST("
            + _quote_identifier(_PACKED_INFO_COLUMN)
            + " AS BLOB)) > ? THEN 1 ELSE 0 END AS packed_truncated, "
            + "typeof("
            + _quote_identifier(_PACKED_INFO_COLUMN)
            + ") AS packed_storage_type"
        )
    sql = (
        "SELECT rowid, local_id, local_type, create_time, "
        + body_columns
        + flag_column
        + ", server_id"
        + packed_columns
        + " FROM "
        + _quote_identifier(table)
        + " WHERE rowid=? AND local_id=? AND server_id IS ? LIMIT 2"
    )
    parameters: list[Any] = [MAX_INPUT_BYTES + 1, MAX_INPUT_BYTES]
    if has_packed_info:
        parameters.extend((MAX_PACKED_INFO_BYTES + 1, MAX_PACKED_INFO_BYTES))
    parameters.extend(
        (identity["rowid"], identity["local_id"], identity["server_id"])
    )
    try:
        rows = budget.fetchall(
            connection,
            sql,
            tuple(parameters),
        )
    except sqlite3.DatabaseError:
        raise UnsupportedSchema("unsupported") from None
    if not rows:
        _fail("message_not_found")
    if len(rows) != 1:
        _fail("message_ambiguous")
    return rows[0]


def _normalise_body_row(
    row: tuple[Any, ...],
    identity: Mapping[str, Any],
    has_compression_flag: bool,
) -> tuple[int, int, Any, int | None, bytes | str, int | None]:
    expected_length = 9 if has_compression_flag else 8
    if len(row) < expected_length:
        _fail("unsupported")
    rowid, local_id, local_type, create_time, raw_body, raw_truncated, storage_type = row[:7]
    flag = row[7] if has_compression_flag else None
    actual_server_id = row[8] if has_compression_flag else row[7]
    for value in (rowid, local_id, local_type, create_time):
        if type(value) is not int or not _MIN_SQL_INT <= value <= _MAX_SQL_INT:
            _fail("unsupported")
    if rowid != identity["rowid"] or local_id != identity["local_id"]:
        _fail("identity_mismatch")
    if actual_server_id != identity["server_id"]:
        _fail("identity_mismatch")
    if type(raw_truncated) is not int or raw_truncated not in (0, 1):
        _fail("unsupported")
    if type(storage_type) is not str:
        _fail("unsupported")
    if raw_body is None:
        _fail("body_missing")
    if isinstance(raw_body, memoryview):
        raw_body = raw_body.tobytes()
    elif isinstance(raw_body, bytearray):
        raw_body = bytes(raw_body)
    if not isinstance(raw_body, (bytes, str)):
        _fail("body_unsupported")
    if raw_truncated or (
        isinstance(raw_body, (bytes, str))
        and len(raw_body) > MAX_INPUT_BYTES
    ):
        _fail("body_too_large")
    if has_compression_flag:
        if flag is not None and type(flag) is not int:
            _fail("body_unavailable")
        if flag is None and storage_type != "text":
            # A BLOB with no explicit codec marker is never guessed as UTF-8.
            _fail("body_unsupported")
    elif storage_type != "text":
        _fail("body_unsupported")
    if isinstance(raw_body, bytes) and len(raw_body) > MAX_INPUT_BYTES:
        _fail("body_too_large")
    return rowid, local_id, raw_body, flag, storage_type, create_time


def _normalise_packed_row(
    row: tuple[Any, ...],
    has_compression_flag: bool,
    has_packed_info: bool,
) -> bytes:
    if not has_packed_info:
        _fail("image_packed_info_missing")
    base_length = 9 if has_compression_flag else 8
    if len(row) != base_length + 3:
        _fail("unsupported")
    raw_packed, raw_truncated, storage_type = row[base_length:]
    if raw_packed is None:
        _fail("packed_info_missing")
    if type(raw_truncated) is not int or raw_truncated not in (0, 1):
        _fail("packed_info_unsupported")
    if type(storage_type) is not str or storage_type != "blob":
        _fail("packed_info_unsupported")
    if isinstance(raw_packed, memoryview):
        raw_packed = raw_packed.tobytes()
    elif isinstance(raw_packed, bytearray):
        raw_packed = bytes(raw_packed)
    if not isinstance(raw_packed, bytes):
        _fail("packed_info_unsupported")
    if raw_truncated or len(raw_packed) > MAX_PACKED_INFO_BYTES:
        _fail("packed_info_too_large")
    if not raw_packed:
        _fail("packed_info_missing")
    return raw_packed


def _parse_declaration(
    text: str,
    *,
    local_type: int,
    budget: _QueryBudget,
    image: bool = False,
    group_image: bool = False,
) -> dict[str, Any]:
    if type(text) is not str:
        _fail("body_unavailable")
    try:
        module_name = (
            "readstore_image_declaration" if image else "readstore_file_declaration"
        )
        function_name = "parse_image_declaration" if image else "parse_file_declaration"
        parser_module = importlib.import_module(f"{__package__}.{module_name}")
        parser = getattr(parser_module, function_name)
        if image:
            declaration = parser(
                text,
                local_type=local_type,
                allow_group_sender_line=group_image,
            )
        else:
            declaration = parser(text, local_type=local_type)
    except Exception:
        raise ReadStoreError("declaration_invalid") from None
    if not isinstance(declaration, Mapping):
        _fail("declaration_invalid")
    try:
        bounded = dict(declaration)
        encoded = json.dumps(
            bounded,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail("declaration_invalid")
    if len(encoded) > _MAX_DECLARATION_JSON_BYTES:
        _fail("declaration_too_large")
    budget._check()
    return bounded


def _parse_image_locator_bounded(
    packed_info_data: bytes,
    budget: _QueryBudget,
) -> dict[str, str]:
    try:
        locator = parse_image_locator(packed_info_data)
    except ImageLocatorError as error:
        if error.code == "packed_info_too_large":
            _fail("packed_info_too_large")
        _fail("image_locator_invalid")
    if (
        type(locator) is not dict
        or set(locator) != {"candidate_basename"}
        or type(locator.get("candidate_basename")) is not str
        or _MD5_RE.fullmatch(locator["candidate_basename"]) is None
    ):
        _fail("image_locator_invalid")
    budget._check()
    return dict(locator)


def query_attachment(
    message_connections: Mapping[str, sqlite3.Connection],
    *,
    account_epoch: str | int,
    conversation_key: str,
    message_identity: Mapping[str, Any],
    deadline: int | float,
    expected_account_epoch: str | int | None = None,
) -> dict[str, Any]:
    """Return one exact message's bounded file declaration.

    ``message_identity`` is a shard-local namespace.  ``rowid``, ``local_id``
    and ``server_id`` are all matched in SQL; an equal ``local_id`` in another
    shard is a different message.  The returned declaration is parser output,
    not a claim that file bytes have been read or verified.
    """

    account_epoch = _validate_account_epoch(account_epoch)
    if expected_account_epoch is not None:
        expected_account_epoch = _validate_account_epoch(expected_account_epoch)
        if expected_account_epoch != account_epoch:
            _fail("account_epoch_mismatch", invalid=True)
    connections = _normalise_connections(message_connections)
    conversation_key = _validate_conversation_key(conversation_key)
    identity = _validate_identity(message_identity)
    if identity["shard_id"] not in connections:
        _fail("invalid_message_identity", invalid=True)
    expected_md5 = hashlib.md5(conversation_key.encode("utf-8")).hexdigest()
    if identity["chat_md5"] != expected_md5:
        _fail("identity_mismatch", invalid=True)

    budget = _make_budget(deadline)
    connection = connections[identity["shard_id"]]
    table, has_compression_flag, has_packed_info = _table_and_schema(
        connection, identity["chat_md5"], budget
    )
    row = _read_exact_row(
        connection,
        table,
        identity,
        has_compression_flag,
        has_packed_info,
        budget,
    )
    _, _, raw_body, compression_flag, _storage_type, create_time = _normalise_body_row(
        row, identity, has_compression_flag
    )
    is_image = row[2] == 3
    packed_info_data = (
        _normalise_packed_row(row, has_compression_flag, has_packed_info)
        if is_image
        else None
    )
    budget._check()
    decoded = decode_body(
        raw_body,
        0 if compression_flag is None else compression_flag,
        deadline=budget.deadline,
        max_output_bytes=MAX_OUTPUT_BYTES,
    )
    budget._check()
    if not decoded.available or decoded.text is None:
        if decoded.code in {
            "output_too_large",
            "output_bound_exceeded",
            "output_ratio_exceeded",
        }:
            _fail("body_too_large")
        _fail("body_unavailable")
    if type(decoded.output_bytes) is not int or decoded.output_bytes > MAX_OUTPUT_BYTES:
        _fail("body_too_large")
    declaration = _parse_declaration(
        decoded.text,
        local_type=row[2],
        budget=budget,
        image=is_image,
        group_image=is_image and conversation_key.endswith("@chatroom"),
    )
    result = {
        "message_identity": dict(identity),
        "account_epoch": account_epoch,
        "declaration": declaration,
        "create_time": create_time,
    }
    if is_image:
        assert packed_info_data is not None
        result["cache_locator"] = _parse_image_locator_bounded(
            packed_info_data, budget
        )
    return result


__all__ = [
    "AttachmentQueryError",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_BYTES",
    "query_attachment",
]
