"""Pure contract candidate for a bounded multi-conversation message read.

This module deliberately stops at the public shape.  It does not locate a
Weixin process, open a database, navigate a window, or dispatch an MCP call.
The authenticated batch provider reuses the per-conversation incremental
read-store cursor while the public result keeps account and conversation
identity attached to every row.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from .sender_role import SENDER_ROLE_KEYS, validate_sender_role_fields


_MIN_INT = -(1 << 63)
_MAX_INT = (1 << 63) - 1
_MAX_EPOCH_CHARS = 256
_MAX_CONVERSATIONS = 16
_MAX_LIMIT = 100
_MAX_TOTAL = 1000
_MAX_CONVERSATION_CHARS = 1024
_MAX_CONVERSATION_BYTES = 4096
_MAX_CONTENT_CHARS = 4096
_MAX_CONTENT_BYTES = 4 * _MAX_CONTENT_CHARS
_MAX_SHARDS = 64
_MAX_FILTER_CHARS = 320

_SHARD_RE = re.compile(r"message_(0|[1-9][0-9]{0,3})\Z")
_MD5_RE = re.compile(r"[0-9a-f]{32}\Z")

_REQUEST_ITEM_KEYS = frozenset(("conversation_key", "cursor", "start_from", "limit"))
_CURSOR_KEYS = frozenset(
    (
        "version",
        "chat_md5",
        "direction",
        "filter",
        "shard_ids",
        "account_epoch",
        "shard_highwater",
        "shard_boundary",
        "gap_detected",
    )
)
_POSITION_KEYS = frozenset(("shard_id", "sort_seq", "local_id", "rowid"))
_RESULT_KEYS = frozenset(
    (
        "ok",
        "status",
        "verification_level",
        "account_epoch",
        "conversations",
        "counts",
        "coverage",
        "ordering",
        "bounded",
        "full_history",
        "exact_once",
    )
)
_STATE_KEYS = frozenset(
    ("conversation_key", "items", "next_cursor", "has_more", "gap_detected")
)
_COUNT_KEYS = frozenset(("conversations", "items"))
_LEGACY_ITEM_KEYS = frozenset(
    (
        "conversation_key",
        "account_epoch",
        "shard_id",
        "local_id",
        "sort_seq",
        "local_type",
        "sender_id",
        "create_time",
        "server_id",
        "content",
        "content_available",
        "content_truncated",
        "message_identity",
    )
)
_ITEM_KEYS = _LEGACY_ITEM_KEYS | SENDER_ROLE_KEYS
_IDENTITY_KEYS = frozenset(("chat_md5", "shard_id", "local_id", "server_id", "rowid"))


class BatchReadContractError(ValueError):
    """Stable local validation error; no provider text is serialized."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str = "batch_read_input_invalid") -> None:
    raise BatchReadContractError(code)


def _strict_int(value: Any) -> bool:
    return type(value) is int and _MIN_INT <= value <= _MAX_INT


def _strict_epoch(value: Any) -> bool:
    if type(value) is int:
        return _MIN_INT <= value <= _MAX_INT
    return (
        type(value) is str
        and 1 <= len(value) <= _MAX_EPOCH_CHARS
        and "\x00" not in value
    )


def _strict_text(value: Any, *, maximum: int, maximum_bytes: int, nonempty: bool = False) -> bool:
    if type(value) is not str or len(value) > maximum or "\x00" in value:
        return False
    if nonempty and not value:
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= maximum_bytes
    except UnicodeError:
        return False


def _conversation_key(value: Any) -> bool:
    return _strict_text(
        value,
        maximum=_MAX_CONVERSATION_CHARS,
        maximum_bytes=_MAX_CONVERSATION_BYTES,
        nonempty=True,
    )


def _shard(value: Any) -> bool:
    return type(value) is str and _SHARD_RE.fullmatch(value) is not None


def _md5(value: Any) -> bool:
    return type(value) is str and _MD5_RE.fullmatch(value) is not None


def _position(value: Any, expected_shard: str) -> bool:
    if type(value) is not dict or set(value) != _POSITION_KEYS:
        return False
    if value["shard_id"] != expected_shard or not _shard(value["shard_id"]):
        return False
    values = (value["sort_seq"], value["local_id"], value["rowid"])
    if all(item is None for item in values):
        return True
    return all(_strict_int(item) for item in values)


def _cursor(value: Any, *, conversation_key: str, account_epoch: Any) -> dict[str, Any] | None:
    """Validate an incremental v3 cursor and bind it to one exact room."""

    if value is None:
        return None
    if type(value) is not dict or set(value) != _CURSOR_KEYS:
        _fail("invalid_batch_cursor")
    if value["version"] != 3 or type(value["version"]) is not int:
        _fail("invalid_batch_cursor")
    if value["direction"] != "asc" or type(value["direction"]) is not str:
        _fail("invalid_batch_cursor")
    if value["gap_detected"] not in (True, False) or type(value["gap_detected"]) is not bool:
        _fail("invalid_batch_cursor")
    chat_md5 = hashlib.md5(conversation_key.encode("utf-8")).hexdigest()
    if value["chat_md5"] != chat_md5 or not _md5(value["chat_md5"]):
        _fail("cursor_conversation_mismatch")
    if value["account_epoch"] != account_epoch or not _strict_epoch(value["account_epoch"]):
        _fail("cursor_epoch_mismatch")
    filter_value = value["filter"]
    if filter_value is not None and (
        type(filter_value) is not str or filter_value != "start_from:now"
    ):
        _fail("invalid_batch_cursor")
    raw_shards = value["shard_ids"]
    if (
        type(raw_shards) is not list
        or not 1 <= len(raw_shards) <= _MAX_SHARDS
        or any(not _shard(item) for item in raw_shards)
        or len(set(raw_shards)) != len(raw_shards)
        or raw_shards != sorted(raw_shards)
    ):
        _fail("invalid_batch_cursor")
    for field in ("shard_highwater", "shard_boundary"):
        positions = value[field]
        if type(positions) is not list or len(positions) != len(raw_shards):
            _fail("invalid_batch_cursor")
        if any(not _position(item, shard) for item, shard in zip(positions, raw_shards)):
            _fail("invalid_batch_cursor")
    return value


def validate_batch_read_request(
    account_epoch: Any,
    conversations: Any,
    *,
    max_total: Any = 256,
) -> dict[str, Any]:
    """Validate a bounded set of exact conversation continuations.

    ``start_from`` is ``now`` or ``beginning`` for a fresh read and ``cursor``
    when the supplied incremental cursor is continued.  The returned object is
    detached from the caller and contains no provider-private fields.
    """

    if not _strict_epoch(account_epoch):
        _fail()
    if type(max_total) is not int or not 1 <= max_total <= _MAX_TOTAL:
        _fail("batch_budget_invalid")
    if (
        type(conversations) is not list
        or not 1 <= len(conversations) <= _MAX_CONVERSATIONS
    ):
        _fail("conversation_count_invalid")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    requested_total = 0
    for value in conversations:
        if type(value) is not dict or set(value) != _REQUEST_ITEM_KEYS:
            _fail()
        key = value["conversation_key"]
        if not _conversation_key(key):
            _fail("conversation_key_invalid")
        if key in seen:
            _fail("duplicate_conversation")
        seen.add(key)
        limit = value["limit"]
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            _fail("batch_limit_invalid")
        cursor_value = value["cursor"]
        start_from = value["start_from"]
        if cursor_value is None:
            if start_from not in ("now", "beginning") or type(start_from) is not str:
                _fail("start_from_invalid")
        else:
            if start_from != "cursor" or type(start_from) is not str:
                _fail("start_from_invalid")
            cursor_value = _cursor(
                cursor_value,
                conversation_key=key,
                account_epoch=account_epoch,
            )
        requested_total += limit
        normalized.append(
            {
                "conversation_key": key,
                "cursor": cursor_value,
                "start_from": start_from,
                "limit": limit,
            }
        )
    if requested_total > max_total:
        _fail("batch_budget_exceeded")
    return {
        "account_epoch": account_epoch,
        "conversations": normalized,
        "max_total": max_total,
    }


def _message_item(value: Any, *, conversation_key: str, account_epoch: Any) -> bool:
    if type(value) is not dict or set(value) not in (_LEGACY_ITEM_KEYS, _ITEM_KEYS):
        return False
    if set(value) == _ITEM_KEYS:
        try:validate_sender_role_fields({key:value[key] for key in SENDER_ROLE_KEYS})
        except (TypeError,ValueError):return False
    if value["conversation_key"] != conversation_key or value["account_epoch"] != account_epoch:
        return False
    if not _shard(value["shard_id"]):
        return False
    if not _strict_int(value["local_id"]) or not _strict_int(value["sort_seq"]):
        return False
    for field in ("local_type", "sender_id", "create_time", "server_id"):
        if value[field] is not None and not _strict_int(value[field]):
            return False
    if value["content"] is not None and not _strict_text(
        value["content"],
        maximum=_MAX_CONTENT_CHARS,
        maximum_bytes=_MAX_CONTENT_BYTES,
    ):
        return False
    if type(value["content_available"]) is not bool or type(value["content_truncated"]) is not bool:
        return False
    if value["content_available"] and (
        value["content"] is None or value["content_truncated"]
    ):
        return False
    identity = value["message_identity"]
    if type(identity) is not dict or set(identity) != _IDENTITY_KEYS:
        return False
    expected_chat = hashlib.md5(conversation_key.encode("utf-8")).hexdigest()
    if identity["chat_md5"] != expected_chat or not _md5(identity["chat_md5"]):
        return False
    if identity["shard_id"] != value["shard_id"] or not _shard(identity["shard_id"]):
        return False
    if identity["local_id"] != value["local_id"] or not _strict_int(identity["local_id"]):
        return False
    if identity["server_id"] != value["server_id"]:
        return False
    return _strict_int(identity["rowid"])


def valid_batch_read_result(result: Any, request: Any) -> bool:
    """Return whether one projected batch result matches its request.

    The validator is intentionally boolean at the public boundary: callers
    must not expose provider exceptions, paths, SQL, or raw UI text as a
    substitute for a verified result.
    """

    if type(request) is not dict or set(request) != {"account_epoch", "conversations", "max_total"}:
        return False
    try:
        parsed_request = validate_batch_read_request(
            request["account_epoch"],
            request["conversations"],
            max_total=request["max_total"],
        )
    except BatchReadContractError:
        return False
    if type(result) is not dict or set(result) != _RESULT_KEYS:
        return False
    if result["ok"] is not True or type(result["ok"]) is not bool:
        return False
    if result["status"] not in ("complete", "partial") or type(result["status"]) is not str:
        return False
    if result["verification_level"] != "authenticated_readstore_batch":
        return False
    if result["account_epoch"] != parsed_request["account_epoch"]:
        return False
    if result["coverage"] != "bounded_authenticated_batch" or result["ordering"] != "rowid_continuation_not_chronological":
        return False
    if result["bounded"] is not True or result["full_history"] is not False or result["exact_once"] is not False:
        return False
    if type(result["conversations"]) is not list or len(result["conversations"]) != len(parsed_request["conversations"]):
        return False
    counts = result["counts"]
    if type(counts) is not dict or set(counts) != _COUNT_KEYS:
        return False
    if type(counts["conversations"]) is not int or type(counts["items"]) is not int:
        return False
    if counts["conversations"] != len(parsed_request["conversations"]):
        return False

    expected_keys = [item["conversation_key"] for item in parsed_request["conversations"]]
    seen_keys: set[str] = set()
    total_items = 0
    any_partial = False
    for state, spec in zip(result["conversations"], parsed_request["conversations"]):
        if type(state) is not dict or set(state) != _STATE_KEYS:
            return False
        key = spec["conversation_key"]
        if state["conversation_key"] != key or key in seen_keys:
            return False
        seen_keys.add(key)
        if type(state["items"]) is not list or len(state["items"]) > spec["limit"]:
            return False
        if type(state["has_more"]) is not bool or type(state["gap_detected"]) is not bool:
            return False
        next_cursor = state["next_cursor"]
        # The existing incremental read projection always carries a native
        # continuation cursor, including a caught-up page with has_more=false.
        if next_cursor is None:
            return False
        if next_cursor is not None:
            try:
                _cursor(next_cursor, conversation_key=key, account_epoch=parsed_request["account_epoch"])
            except BatchReadContractError:
                return False
            if next_cursor["gap_detected"] != state["gap_detected"]:
                return False
        identities: set[tuple[str, int]] = set()
        for item in state["items"]:
            if not _message_item(item, conversation_key=key, account_epoch=parsed_request["account_epoch"]):
                return False
            identity_key = (item["shard_id"], item["message_identity"]["rowid"])
            if identity_key in identities:
                return False
            identities.add(identity_key)
        total_items += len(state["items"])
        any_partial = any_partial or state["has_more"] or state["gap_detected"]
    if expected_keys != [state["conversation_key"] for state in result["conversations"]]:
        return False
    if counts["items"] != total_items or total_items > parsed_request["max_total"]:
        return False
    if result["status"] != ("partial" if any_partial else "complete"):
        return False
    return True


__all__ = [
    "BatchReadContractError",
    "validate_batch_read_request",
    "valid_batch_read_result",
]
