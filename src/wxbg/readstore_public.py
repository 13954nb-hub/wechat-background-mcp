"""Safe public boundary for the bounded read-store inbox result.

The provider owns the authenticated in-memory SQLite snapshots.  This module
only validates its already materialized response and returns a small public
projection.  It never opens a database, handles keys, or exposes provider
diagnostics and source evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError


MAX_LIMIT = 100
MAX_TEXT_CHARS = 4096
MAX_USERNAME_CHARS = 1024
MAX_ACCOUNT_EPOCH_CHARS = 256
MAX_PUBLIC_RESULT_BYTES = 384 * 1024
MAX_TIMESTAMP = (1 << 63) - 1
MIN_TIMESTAMP = -(1 << 63)
MAX_UNREAD_COUNT = (1 << 63) - 1

_PUBLIC_ERROR = {
    "code": "readstore_result_invalid",
    "outcome_unknown": False,
    "retry": False,
}
_COVERAGE = "sessiontable_contact_bounded_nonhidden"
_ALL_COVERAGE = "sessiontable_contact_bounded_all"
_ORDER = "sort_timestamp_desc_username_asc"
_CURSOR_KEYS = frozenset({
    "version", "account_epoch", "unread_only", "order", "sort_timestamp", "username",
})
_ROW_KEYS = frozenset({
    "conversation_key", "display_name", "display_source",
    "display_available", "display_truncated", "unread_count", "unread_known",
    "summary", "summary_available", "summary_truncated",
    "last_timestamp", "sort_timestamp",
})
_DISPLAY_SOURCES = frozenset({"remark", "nick_name", "username"})


def _invalid() -> None:
    """Raise one fixed error without serializing any provider-controlled data."""

    raise ToolError(json.dumps(_PUBLIC_ERROR, ensure_ascii=True, separators=(",", ":")))


def _strict_text(value: Any, *, maximum: int, nonempty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or "\x00" in value:
        _invalid()
    if nonempty and not value:
        _invalid()
    return value


def _strict_bool(value: Any) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def _strict_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not MIN_TIMESTAMP <= value <= MAX_TIMESTAMP:
        _invalid()
    return value


def _strict_epoch(value: Any) -> str | int:
    if type(value) is int:
        return value
    if type(value) is str and 1 <= len(value) <= MAX_ACCOUNT_EPOCH_CHARS and "\x00" not in value:
        return value
    _invalid()


def _background_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _invalid()
    if value.get("background_observation_passed") is not True:
        _invalid()
    if (type(value.get("observations")) is not int
            or value["observations"] < 1):
        _invalid()
    for key in (
        "foreground_changed", "clipboard_changed", "target_restored",
        "capture_observed",
    ):
        if value.get(key) is not False:
            _invalid()
    if type(value.get("cursor_changed")) is not bool:
        _invalid()
    if value.get("new_visible_windows") != []:
        _invalid()
    if value.get("monitor_errors") != []:
        _invalid()
    return {
        "background_observation_passed": True,
        "observations": value["observations"],
        "foreground_changed": False,
        "clipboard_changed": False,
        "cursor_changed": value["cursor_changed"],
        "target_restored": False,
        "capture_observed": False,
        "new_visible_windows": [],
        "monitor_errors": [],
    }


def _cleanup_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _invalid()
    if (value.get("status") != "read_only"
            or value.get("restored") is not True
            or value.get("modified") is not False
            or value.get("gate_touched") is not False
            or value.get("errors") != []):
        _invalid()
    return {
        "status": "read_only",
        "restored": True,
        "modified": False,
        "gate_touched": False,
        "errors": [],
    }


def _validate_capture(value: Any) -> None:
    if not isinstance(value, Mapping):
        _invalid()
    if (value.get("consistency") != "observed_quiet_window"
            or value.get("matching_passes") != 2
            or value.get("same_handles") is not True
            or value.get("path_identity_rechecked") is not True
            or value.get("atomic_snapshot") is not False):
        _invalid()
    if (type(value.get("matching_passes")) is not int
            or type(value.get("same_handles")) is not bool
            or type(value.get("path_identity_rechecked")) is not bool
            or type(value.get("atomic_snapshot")) is not bool):
        _invalid()


def _validate_snapshot(value: Any) -> None:
    if not isinstance(value, Mapping):
        _invalid()
    if (value.get("all_applied_page_hmac_verified") is not True
            or value.get("integrity_verified") is not True
            or value.get("source_consistency") != "caller_must_verify_capture"):
        _invalid()


def _validate_readstore_evidence(value: Any) -> None:
    if not isinstance(value, Mapping):
        _invalid()
    if (value.get("multi_database_atomic") is not False
            or value.get("source_consistency") != "observed_quiet_window"):
        _invalid()
    captures = value.get("captures")
    snapshots = value.get("snapshots")
    if not isinstance(captures, Mapping) or not isinstance(snapshots, Mapping):
        _invalid()
    for name in ("session", "contact"):
        if name not in captures or name not in snapshots:
            _invalid()
        _validate_capture(captures[name])
        _validate_snapshot(snapshots[name])


def _validate_row(value: Any, *, include_hidden: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not _ROW_KEYS <= set(value):
        _invalid()
    conversation_key = _strict_text(
        value.get("conversation_key"), maximum=MAX_USERNAME_CHARS, nonempty=True
    )
    display_name = _strict_text(
        value.get("display_name"), maximum=MAX_TEXT_CHARS, nonempty=True
    )
    display_source = value.get("display_source")
    if type(display_source) is not str or display_source not in _DISPLAY_SOURCES:
        _invalid()
    display_available = _strict_bool(value.get("display_available"))
    display_truncated = _strict_bool(value.get("display_truncated"))
    if display_available and display_truncated:
        _invalid()

    unread_count = value.get("unread_count")
    if unread_count is not None and (
        type(unread_count) is not int
        or not 0 <= unread_count <= MAX_UNREAD_COUNT
    ):
        _invalid()
    unread_known = _strict_bool(value.get("unread_known"))
    if unread_known is not (unread_count is not None):
        _invalid()

    summary = value.get("summary")
    if summary is not None:
        summary = _strict_text(summary, maximum=MAX_TEXT_CHARS)
    summary_available = _strict_bool(value.get("summary_available"))
    summary_truncated = _strict_bool(value.get("summary_truncated"))
    if summary_available and (summary is None or summary_truncated):
        _invalid()

    last_timestamp = _strict_timestamp(value.get("last_timestamp"))
    sort_timestamp = _strict_timestamp(value.get("sort_timestamp"))
    projected = {
        "conversation_key": conversation_key,
        "display_name": display_name,
        "display_source": display_source,
        "display_available": display_available,
        "display_truncated": display_truncated,
        "unread_count": unread_count,
        "unread_known": unread_known,
        "summary": summary,
        "summary_available": summary_available,
        "summary_truncated": summary_truncated,
        "last_timestamp": last_timestamp,
        "sort_timestamp": sort_timestamp,
    }
    if include_hidden:
        if "is_hidden" not in value or value["is_hidden"] is not None and type(value["is_hidden"]) is not bool:
            _invalid()
        projected["is_hidden"] = value["is_hidden"]
    return projected


def _validate_cursor(value: Any, *, unread_only: bool,
                     include_hidden: bool = False) -> dict[str, Any] | None:
    if value is None:
        return None
    required = _CURSOR_KEYS | ({"include_hidden"} if include_hidden else set())
    if not isinstance(value, Mapping) or not required <= set(value):
        _invalid()
    if type(value.get("version")) is not int or value["version"] != (2 if include_hidden else 1):
        _invalid()
    if include_hidden and value.get("include_hidden") is not True:
        _invalid()
    epoch = _strict_epoch(value.get("account_epoch"))
    if type(value.get("unread_only")) is not bool or value["unread_only"] is not unread_only:
        _invalid()
    if value.get("order") != _ORDER:
        _invalid()
    sort_timestamp = _strict_timestamp(value.get("sort_timestamp"))
    username = _strict_text(
        value.get("username"), maximum=MAX_USERNAME_CHARS, nonempty=True
    )
    projected = {
        "version": 2 if include_hidden else 1,
        "account_epoch": epoch,
        "unread_only": unread_only,
        "order": _ORDER,
        "sort_timestamp": sort_timestamp,
        "username": username,
    }
    if include_hidden:
        projected["include_hidden"] = True
    return projected


def _row_after(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Check the same NULL-last, timestamp-desc/name-asc order as read_inbox."""

    if current["conversation_key"] == previous["conversation_key"]:
        return False
    previous_timestamp = previous["sort_timestamp"]
    current_timestamp = current["sort_timestamp"]
    if previous_timestamp is None:
        return current_timestamp is None and current["conversation_key"] > previous["conversation_key"]
    if current_timestamp is None:
        return True
    if current_timestamp < previous_timestamp:
        return True
    return (current_timestamp == previous_timestamp
            and current["conversation_key"] > previous["conversation_key"])


def _project_inbox(result: Any, *, limit: int, unread_only: bool,
                   include_hidden: bool = False) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        _invalid()
    for key in ("conversations", "next_cursor", "has_more", "coverage", "readstore_evidence"):
        if key not in result:
            _invalid()
    coverage = _ALL_COVERAGE if include_hidden else _COVERAGE
    if result.get("coverage") != coverage:
        _invalid()
    _validate_readstore_evidence(result.get("readstore_evidence"))

    conversations = result.get("conversations")
    if not isinstance(conversations, list) or len(conversations) > limit:
        _invalid()
    clean_rows = [_validate_row(row, include_hidden=include_hidden) for row in conversations]
    seen_keys: set[str] = set()
    for row in clean_rows:
        if row["conversation_key"] in seen_keys:
            _invalid()
        seen_keys.add(row["conversation_key"])
    if unread_only:
        for row in clean_rows:
            if (row["unread_known"] is not True
                    or type(row["unread_count"]) is not int
                    or row["unread_count"] <= 0):
                _invalid()
    for previous, current in zip(clean_rows, clean_rows[1:]):
        if not _row_after(previous, current):
            _invalid()

    has_more = _strict_bool(result.get("has_more"))
    next_cursor = _validate_cursor(result.get("next_cursor"), unread_only=unread_only,
                                   include_hidden=include_hidden)
    if has_more:
        if not clean_rows or next_cursor is None:
            _invalid()
        last = clean_rows[-1]
        if (next_cursor["sort_timestamp"] != last["sort_timestamp"]
                or next_cursor["username"] != last["conversation_key"]):
            _invalid()
    elif next_cursor is not None:
        _invalid()

    projected = {
        "conversations": clean_rows,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "coverage": coverage,
    }
    try:
        encoded = json.dumps(projected, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _invalid()
    if len(encoded) > MAX_PUBLIC_RESULT_BYTES:
        _invalid()
    return projected


def _raise_verified_background_side_effect(response: Any) -> None:
    """Preserve only the fixed code for a verified read-only monitor rejection."""
    if (isinstance(response, Mapping) and response.get("ok") is False
            and response.get("worker_started") is True
            and "result" not in response
            and isinstance(response.get("target_validation"), Mapping)
            and response["target_validation"].get("status") == "stable"
            and isinstance(response.get("error"), Mapping)
            and response["error"].get("code") == "background_side_effect"
            and response["error"].get("outcome_unknown") is False
            and response["error"].get("submission_started") is False
            and isinstance(response.get("evidence"), Mapping)
            and response["evidence"].get("background_observation_passed") is False):
        _cleanup_projection(response.get("cleanup"))
        raise ToolError(json.dumps({"code": "background_side_effect",
                                    "outcome_unknown": False, "retry": False},
                                   ensure_ascii=True, separators=(",", ":")))


_PUBLIC_READSTORE_ERRORS = frozenset({
    "readstore_provider_unavailable", "readstore_provider_failed",
    "readstore_result_invalid", "readstore_invalid_deadline",
    "readstore_deadline", "readstore_background_failed",
    "readstore_input_invalid", "readstore_size_limit",
    "readstore_source_changed", "readstore_account_changed",
    "readstore_unavailable", "readstore_index_invalid",
    "query_budget_exceeded", "unsupported",
    "invalid_cursor", "cursor_context_conflict", "invalid_chat_md5",
    "missing_epoch", "schema_unsupported",
})


def _raise_verified_readstore_error(
    response: Any, *, allowed_codes: frozenset[str] | None = None
) -> None:
    """Preserve a fixed worker error only with complete read-only evidence."""
    if (type(response) is not dict or response.get("ok") is not False
            or response.get("worker_started") is not True
            or "result" in response
            or type(response.get("target_validation")) is not dict
            or response["target_validation"].get("status") != "stable"
            or type(response.get("error")) is not dict):
        return
    error = response["error"]
    code = error.get("code")
    codes = _PUBLIC_READSTORE_ERRORS if allowed_codes is None else allowed_codes
    if (type(code) is not str or code not in codes
            or error.get("outcome_unknown") is not False
            or error.get("submission_started", False) is not False):
        return
    _background_projection(response.get("evidence"))
    _cleanup_projection(response.get("cleanup"))
    raise ToolError(json.dumps({"code": code, "outcome_unknown": False,
                                "retry": False},
                               ensure_ascii=True, separators=(",", ":")))


def inbox_result_or_error(response: Any, *, limit: Any, unread_only: Any,
                          include_hidden: Any = False) -> dict[str, Any]:
    """Validate a worker/provider response and return its safe public projection."""

    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        _invalid()
    if type(unread_only) is not bool:
        _invalid()
    if type(include_hidden) is not bool:
        _invalid()
    _raise_verified_background_side_effect(response)
    _raise_verified_readstore_error(response)
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        _invalid()
    if response.get("worker_started") is not True:
        _invalid()
    target_validation = response.get("target_validation")
    if not isinstance(target_validation, Mapping) or target_validation.get("status") != "stable":
        _invalid()
    background = _background_projection(response.get("evidence"))
    cleanup = _cleanup_projection(response.get("cleanup"))
    result = _project_inbox(response.get("result"), limit=limit,
                            unread_only=unread_only, include_hidden=include_hidden)
    return {"ok": True, "result": result, "evidence": {
        "background": background,
        "cleanup": cleanup,
    }}


__all__ = ["inbox_result_or_error"]
