"""Pure public contract for a verified local image UI transition.

This module deliberately has no UI, native, Weixin, MCP, or worker dependency.
The worker evidence is normalized by ``image_observation_contract`` and only
bounded scalar metadata is projected into the durable journal summary.
"""

from __future__ import annotations

import math
import re

from .image_observation_contract import normalize_image_evidence


INVALID_RESULT_CODE = "image_result_invalid"

_SESSION_REF_RE = re.compile(r"[0-9a-f]{32}\Z")

_PUBLIC_RESULT_KEYS = frozenset(
    (
        "ok",
        "status",
        "verification_level",
        "counts",
        "refs",
        "background_mode",
        "remote_receipt_verified",
        "upload_status",
    )
)
_COUNT_KEYS = frozenset(("native_selections", "send_clicks", "new_rows"))
_REF_KEYS = frozenset(("conversation",))
_RESPONSE_KEYS = frozenset(
    (
        "ok",
        "worker_started",
        "result",
        "evidence",
        "cleanup",
        "image_evidence_valid",
        "image_evidence",
        "native_evidence",
        # These are normal guardian envelope fields; they are not projected.
        "recovery",
        "elapsed_seconds",
    )
)

_NATIVE_INT_VALUES = {
    "matches": 1,
    "accepted_shows": 1,
    "shows": 0,
    "live": 0,
    "installed": 0,
    "active_filter": 0,
    "protection_restored": 1,
    "cleanup_unresolved": 0,
    "error": 0,
}
_NATIVE_BOOL_KEYS = (
    "released",
    "grant_revoked",
    "passed",
    "hook_removed",
    "local_module_released",
    "remote_module_present",
    "submission_started",
)


def _exact_int(value, minimum=None, maximum=None):
    return (
        type(value) is int
        and (minimum is None or value >= minimum)
        and (maximum is None or value <= maximum)
    )


def _valid_session_ref(value):
    return type(value) is str and _SESSION_REF_RE.fullmatch(value) is not None


def _valid_result_shape(value, session_ref=None):
    if type(value) is not dict or set(value) != _PUBLIC_RESULT_KEYS:
        return False
    if value["ok"] is not True or type(value["ok"]) is not bool:
        return False
    if (
        value["status"] != "local_image_transition_observed"
        or type(value["status"]) is not str
        or value["verification_level"] != "stable_local_image_ui_transition"
        or type(value["verification_level"]) is not str
        or value["background_mode"] != "minimized"
        or type(value["background_mode"]) is not str
        or value["remote_receipt_verified"] is not False
        or type(value["remote_receipt_verified"]) is not bool
        or value["upload_status"] != "unknown"
        or type(value["upload_status"]) is not str
    ):
        return False

    counts = value["counts"]
    if type(counts) is not dict or set(counts) != _COUNT_KEYS:
        return False
    if not all(type(counts[key]) is int for key in _COUNT_KEYS):
        return False
    if counts["native_selections"] != 1:
        return False
    if counts["send_clicks"] not in (0, 1):
        return False
    if not 1 <= counts["new_rows"] <= 8:
        return False

    refs = value["refs"]
    if type(refs) is not dict or set(refs) != _REF_KEYS:
        return False
    if not _valid_session_ref(refs["conversation"]):
        return False
    if session_ref is not None:
        if not _valid_session_ref(session_ref) or refs["conversation"] != session_ref:
            return False
    return True


def _valid_native_evidence(value):
    """Mirror image_entry_probe's strict terminal native predicate by types."""
    if type(value) is not dict:
        return False
    required = set(_NATIVE_INT_VALUES) | set(_NATIVE_BOOL_KEYS) | {
        "primary_error", "cleanup_errors"
    }
    if not required.issubset(value):
        return False
    for key, expected in _NATIVE_INT_VALUES.items():
        if type(value.get(key)) is not int or value[key] != expected:
            return False
    if any(type(value.get(key)) is not bool or value[key] is not True
           for key in _NATIVE_BOOL_KEYS):
        return False
    if value.get("primary_error") is not None:
        return False
    if type(value.get("cleanup_errors")) is not list or value["cleanup_errors"] != []:
        return False
    return True


def _valid_cleanup(value):
    if type(value) is not dict:
        return False
    return (
        value.get("restored") is True
        and type(value.get("restored")) is bool
        and type(value.get("observed")) is int
        and value["observed"] == 0
        and type(value.get("errors")) is list
        and value["errors"] == []
    )


def _valid_background(value):
    """Validate product-cursor telemetry without importing its monitor."""
    if type(value) is not dict or value.get("background_observation_passed") is not True:
        return False
    if type(value.get("cursor_changed")) is not bool:
        return False
    if any(
        value.get(key) is not False
        for key in (
            "foreground_changed",
            "clipboard_changed",
            "target_restored",
            "capture_observed",
        )
    ):
        return False
    if value.get("new_visible_windows") != [] or value.get("monitor_errors") != []:
        return False
    if type(value.get("observations")) is not int or value["observations"] < 1:
        return False

    for state in (value.get("before"), value.get("after")):
        if type(state) is not dict:
            return False
        cursor = state.get("cursor")
        windows = state.get("visible_windows")
        if (
            state.get("minimized") is not True
            or type(state.get("capture")) is not int
            or state["capture"] != 0
            or type(state.get("foreground")) is not int
            or type(state.get("clipboard_sequence")) is not int
            or type(cursor) is not list
            or len(cursor) != 2
            or any(type(item) is not int for item in cursor)
            or type(windows) is not list
            or any(type(item) is not int for item in windows)
            or state.get("cursor_api") != "GetCursorPos"
            or state.get("cursor_dpi_context") != "per_monitor_v2"
            or state.get("cursor_coordinate_space") != "screen_coordinates_under_pm_v2"
        ):
            return False
    return all(
        value["before"][key] == value["after"][key]
        for key in ("foreground", "clipboard_sequence", "visible_windows")
    )


def _valid_transition_trace(trace, result_value):
    if type(trace) is not dict or type(result_value) is not dict:
        return False
    normalized, valid = normalize_image_evidence(trace)
    if not valid or normalized["baseline"] is None:
        return False

    send_clicks = result_value["counts"]["send_clicks"]
    new_rows = result_value["counts"]["new_rows"]
    stage = "final_frames" if send_clicks == 1 else "selection_frames"
    frames = normalized[stage]
    if len(frames) < 2:
        return False
    previous, last = frames[-2:]
    if any(
        frame["image_novelty"] is not True
        or frame["draft_kind"] != "empty"
        or frame["new_rows_count"] != new_rows
        for frame in (previous, last)
    ):
        return False
    if last["stable_count"] < 2:
        return False
    return (
        previous["runtime_sha256"] == last["runtime_sha256"]
        and previous["semantic_sha256"] == last["semantic_sha256"]
        and previous["row_count"] == last["row_count"]
    )


def _valid_optional_envelope(value):
    if "recovery" in value:
        recovery = value["recovery"]
        if type(recovery) is not dict:
            return False
        if "restored" in recovery and (
            recovery["restored"] is not None and type(recovery["restored"]) is not bool
        ):
            return False
        if "status" in recovery and type(recovery["status"]) is not str:
            return False
    if "elapsed_seconds" in value:
        elapsed = value["elapsed_seconds"]
        if type(elapsed) not in (int, float) or isinstance(elapsed, bool):
            return False
        if not math.isfinite(elapsed) or not 0 <= elapsed <= 120:
            return False
    return True


def valid_image_response(response, session_ref):
    """Return whether a guardian response proves a local image transition."""
    try:
        if type(response) is not dict or not set(response).issubset(_RESPONSE_KEYS):
            return False
        required = (
            "ok",
            "worker_started",
            "result",
            "image_evidence_valid",
            "image_evidence",
            "native_evidence",
            "evidence",
            "cleanup",
        )
        if any(key not in response for key in required):
            return False
        if response["ok"] is not True or type(response["ok"]) is not bool:
            return False
        if (response["worker_started"] is not True
                or type(response["worker_started"]) is not bool):
            return False
        if (response["image_evidence_valid"] is not True
                or type(response["image_evidence_valid"]) is not bool):
            return False
        if not _valid_optional_envelope(response):
            return False
        if not _valid_result_shape(response["result"], session_ref):
            return False
        if not _valid_transition_trace(response["image_evidence"], response["result"]):
            return False
        if not _valid_native_evidence(response["native_evidence"]):
            return False
        if not _valid_cleanup(response["cleanup"]):
            return False
        return _valid_background(response["evidence"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def image_journal_summary(result):
    """Return a rebuilt scalar-only summary suitable for a durable Journal."""
    if not _valid_result_shape(result):
        raise ValueError(INVALID_RESULT_CODE)
    return {
        "ok": True,
        "status": "local_image_transition_observed",
        "verification_level": "stable_local_image_ui_transition",
        "counts": {
            "native_selections": result["counts"]["native_selections"],
            "send_clicks": result["counts"]["send_clicks"],
            "new_rows": result["counts"]["new_rows"],
        },
        "refs": {"conversation": result["refs"]["conversation"]},
        "background_mode": "minimized",
        "remote_receipt_verified": False,
        "upload_status": "unknown",
    }


__all__ = ["valid_image_response", "image_journal_summary"]
