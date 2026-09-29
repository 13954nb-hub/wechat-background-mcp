"""Closed content/body contract for the bounded history-span operation.

The worker and supervisor exchange only this fixed typed evidence object across
the process boundary.  The result body is returned once to the caller, while
the journal receives only the metadata projection from
``history_span_journal_summary``.
"""

from __future__ import annotations


SAFE_HISTORY_SPAN_ERRORS = frozenset({
    "invalid_history_span_steps", "invalid_history_span_direction",
    "invalid_history_span_limit", "invalid_session_ref", "invalid_deadline",
    "history_span_budget_exhausted", "history_span_view_not_settled",
    "history_span_failed", "context_conflict", "ambiguous_recipient",
    "draft_conflict", "history_frame_changed", "history_row_changed",
    "history_row_unsupported", "history_row_identity_invalid",
    "history_row_geometry_invalid",
    "history_observation_failed", "history_ancestry_invalid",
    "history_identity_missing", "history_geometry_invalid",
    "history_frame_ambiguous", "history_rows_missing", "history_rows_overlap",
    "unverified_layout", "unverified_geometry", "native_capture_active",
    "capture_observation_failed", "coordinate_context_unverified",
    "coordinate_mapping_unverified", "modifier_or_mouse_button_pressed",
    "stale_process", "stale_window", "background_requires_minimized",
    "wechat_has_foreground", "existing_popup", "invalid_history_wheel",
    "missing_context_guard", "history_wheel_observation_failed",
    "wheel_result_unknown", "history_view_changed", "tree_budget_exceeded",
    "cannot_disable_auto_focus", "payment_excluded",
})

EVIDENCE_KEYS = frozenset({
    "mode", "direction", "requested_steps", "delivery_started",
    "completed_steps", "observed_view_count", "viewport_settled",
    "conversation_preserved", "draft_preserved", "final_view_retained",
    "not_full_history", "boundary_verified", "chronological_order_verified",
    "primary_error_code",
})

_BOOL_EVIDENCE_KEYS = (
    "delivery_started", "viewport_settled", "conversation_preserved",
    "draft_preserved", "final_view_retained", "not_full_history",
    "boundary_verified", "chronological_order_verified",
)
_INT_EVIDENCE_KEYS = ("requested_steps", "completed_steps", "observed_view_count")
_MISSING_EVIDENCE = "worker_history_span_evidence_missing"
_INVALID_EVIDENCE = "history_span_evidence_invalid"

_BODY_KEYS = frozenset({
    "ok", "status", "verification_level", "background_mode", "direction",
    "views", "counts", "refs", "not_full_history", "boundary_verified",
    "chronological_order_verified", "final_view_retained",
})
_COUNT_KEYS = frozenset({
    "requested_steps", "completed_steps", "view_count", "changed_view_count",
})
_VIEW_KEYS = frozenset({
    "offset", "rows", "exposed_count", "returned_count", "viewport_changed",
    "root_bounds", "viewport_bounds",
})
_ROW_KEYS = frozenset({"kind", "text", "bounds"})
_ROW_KINDS = frozenset({
    "mmui::ChatTextItemView", "mmui::ChatItemView", "mmui::ChatBubbleItemView",
    "mmui::ChatBubbleReferItemView",
})
def safe_history_span_error(code):
    """Return only an opaque, documented error code to the caller."""
    if type(code) is str and code in SAFE_HISTORY_SPAN_ERRORS:
        return code
    return "history_span_failed"


def _exact_int(value, minimum=None, maximum=None):
    return (type(value) is int
            and (minimum is None or value >= minimum)
            and (maximum is None or value <= maximum))


def normalize_history_span_evidence(value):
    """Strip untrusted fields and return ``(fixed_value, shape_is_valid)``."""
    fixed = {key: None for key in EVIDENCE_KEYS}
    if not isinstance(value, dict):
        fixed["primary_error_code"] = _MISSING_EVIDENCE
        return fixed, False

    valid = set(value) == EVIDENCE_KEYS
    for key in _BOOL_EVIDENCE_KEYS:
        item = value.get(key)
        if item is None or type(item) is bool:
            fixed[key] = item
        else:
            valid = False
    mode = value.get("mode")
    if mode is None or (type(mode) is str and mode == "intentional_navigation_span"):
        fixed["mode"] = mode
    else:
        valid = False
    direction = value.get("direction")
    if direction is None or (type(direction) is str and direction in ("older", "newer")):
        fixed["direction"] = direction
    else:
        valid = False
    for key, minimum, maximum in (
        ("requested_steps", 1, 4), ("completed_steps", 0, 4),
        ("observed_view_count", 0, 5),
    ):
        item = value.get(key)
        if item is None or _exact_int(item, minimum, maximum):
            fixed[key] = item
        else:
            valid = False
    code = value.get("primary_error_code")
    if code is None or (type(code) is str and code in SAFE_HISTORY_SPAN_ERRORS | {
            _MISSING_EVIDENCE, _INVALID_EVIDENCE}):
        fixed["primary_error_code"] = code
    else:
        valid = False

    if fixed["delivery_started"] is False and fixed["completed_steps"] not in (None, 0):
        valid = False
        fixed["delivery_started"] = None
    if (_exact_int(fixed["requested_steps"], 1, 4)
            and _exact_int(fixed["completed_steps"], 0, 4)
            and fixed["completed_steps"] > fixed["requested_steps"]):
        valid = False
    if (_exact_int(fixed["observed_view_count"], 0, 5)
            and _exact_int(fixed["completed_steps"], 0, 4)
            and fixed["observed_view_count"] != fixed["completed_steps"] + 1):
        # A failed destination observation can follow a delivered wheel, so
        # completed_steps may lead the count of settled views by one.
        if (fixed["observed_view_count"] != fixed["completed_steps"]
                or fixed["primary_error_code"] is None
                or fixed["viewport_settled"] is True):
            valid = False
    if not valid:
        fixed["primary_error_code"] = _INVALID_EVIDENCE
    return fixed, valid


def _bounds(value):
    if not (isinstance(value, list) and len(value) == 4
            and all(type(item) is int for item in value)):
        return None
    left, top, right, bottom = value
    if not (0 < right - left <= 32767 and 0 < bottom - top <= 32767):
        return None
    return left, top, right, bottom


def _inside(inner, outer):
    return (outer[0] <= inner[0] < inner[2] <= outer[2]
            and outer[1] <= inner[1] < inner[3] <= outer[3])


def _validate_row(row_value, viewport):
    if not isinstance(row_value, dict) or set(row_value) != _ROW_KEYS:
        return None
    if type(row_value["kind"]) is not str or row_value["kind"] not in _ROW_KINDS:
        return None
    if type(row_value["text"]) is not str or "\x00" in row_value["text"]:
        return None
    bounds = _bounds(row_value["bounds"])
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    if (left != viewport[0] or right != viewport[2]
            or max(top, viewport[1]) >= min(bottom, viewport[3])):
        return None
    return bounds


def _validate_body_shape(result, *, limit_per_view=None):
    if not isinstance(result, dict) or set(result) != _BODY_KEYS:
        return False
    if (type(result["ok"]) is not bool or result["ok"] is not True
            or result["status"] != "history_span_observed"
            or result["verification_level"] != "settled_visible_span_with_final_destination_retained"
            or result["background_mode"] != "minimized"
            or type(result["direction"]) is not str
            or result["direction"] not in ("older", "newer")
            or result["not_full_history"] is not True
            or result["boundary_verified"] is not False
            or result["chronological_order_verified"] is not False
            or result["final_view_retained"] is not True):
        return False
    counts = result["counts"]
    if not isinstance(counts, dict) or set(counts) != _COUNT_KEYS:
        return False
    if not all(_exact_int(counts[key], 0) for key in _COUNT_KEYS):
        return False
    steps = counts["requested_steps"]
    if not _exact_int(steps, 1, 4) or counts["completed_steps"] != steps:
        return False
    views = result["views"]
    if not isinstance(views, list) or len(views) != counts["view_count"]:
        return False
    if counts["view_count"] != steps + 1 or not 0 <= counts["changed_view_count"] <= steps:
        return False
    changed_count = 0
    observed_geometry = None
    for expected_offset, view in enumerate(views):
        if not isinstance(view, dict) or set(view) != _VIEW_KEYS:
            return False
        root = _bounds(view["root_bounds"])
        viewport = _bounds(view["viewport_bounds"])
        if root is None or viewport is None or not _inside(viewport, root):
            return False
        geometry = root, viewport
        if observed_geometry is not None and geometry != observed_geometry:
            return False
        observed_geometry = geometry
        if (type(view["offset"]) is not int or view["offset"] != expected_offset
                or type(view["exposed_count"]) is not int
                or type(view["returned_count"]) is not int
                or view["exposed_count"] < 1
                or view["returned_count"] < 1
                or view["returned_count"] > view["exposed_count"]):
            return False
        if (limit_per_view is not None
                and view["returned_count"] != min(view["exposed_count"], limit_per_view)):
            return False
        rows = view["rows"]
        if not isinstance(rows, list) or len(rows) != view["returned_count"]:
            return False
        for row_value in rows:
            if _validate_row(row_value, viewport) is None:
                return False
        if type(view["viewport_changed"]) is not bool:
            return False
        if expected_offset == 0 and view["viewport_changed"] is not False:
            return False
        changed_count += int(view["viewport_changed"])
    if changed_count != counts["changed_view_count"]:
        return False
    refs = result["refs"]
    return (isinstance(refs, dict) and set(refs) == {"conversation"}
            and type(refs["conversation"]) is str and bool(refs["conversation"]))


def valid_history_span_success(result, evidence, desktop, args):
    """Validate a success only when body, evidence and independent gates agree."""
    value, shape_valid = normalize_history_span_evidence(evidence)
    if not shape_valid or not isinstance(args, dict) or not isinstance(desktop, dict):
        return False
    steps = args.get("steps", 1)
    direction = args.get("direction")
    reference = args.get("session_ref")
    limit = args.get("limit_per_view", 200)
    if (not _exact_int(steps, 1, 4) or type(direction) is not str
            or direction not in ("older", "newer")
            or type(reference) is not str or not reference
            or not _exact_int(limit, 1, 200)):
        return False
    if (value["mode"] != "intentional_navigation_span"
            or value["direction"] != direction
            or value["requested_steps"] != steps
            or value["completed_steps"] != steps
            or value["observed_view_count"] != steps + 1
            or any(value[key] is not True for key in (
                "delivery_started", "viewport_settled", "conversation_preserved",
                "draft_preserved", "final_view_retained", "not_full_history"))
            or value["boundary_verified"] is not False
            or value["chronological_order_verified"] is not False
            or value["primary_error_code"] is not None):
        return False
    if not _validate_body_shape(result, limit_per_view=limit):
        return False
    counts = result["counts"]
    return (counts["requested_steps"] == steps
            and counts["completed_steps"] == value["completed_steps"]
            and counts["view_count"] == value["observed_view_count"]
            and result["direction"] == direction
            and result["refs"] == {"conversation": reference}
            and isinstance(desktop, dict)
            and desktop.get("background_observation_passed") is True)


def history_span_journal_summary(result):
    """Return the only result projection permitted in the durable journal."""
    if not _validate_body_shape(result):
        raise ValueError("history_span_result_invalid")
    return {
        "ok": True,
        "status": result["status"],
        "verification_level": result["verification_level"],
        "counts": dict(result["counts"]),
        "refs": dict(result["refs"]),
        "background_mode": result["background_mode"],
    }
