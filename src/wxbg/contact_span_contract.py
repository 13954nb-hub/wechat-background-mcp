"""Closed body/evidence contract for the bounded Contacts span operation."""

from __future__ import annotations


SAFE_CONTACT_SPAN_ERRORS = frozenset({
    "TARGET_NOT_VISIBLE", "TARGET_AMBIGUOUS",
    # Existing contact_actions/contact_scroll/contact_wheel codes.
    "contacts_deadline", "context_conflict", "tree_budget_exceeded",
    "unverified_layout", "sidebar_tab_changed", "ambiguous_recipient",
    "observation_failed", "draft_conflict", "ambiguous_contacts_table",
    "contacts_page_changed", "contact_outside_table", "contact_outside_bounds",
    "contact_changed", "ambiguous_contact_identity", "original_chat_not_restored",
    "invalid_query", "invalid_limit", "invalid_scroll_steps", "contacts_not_opened",
    "navigation_restore_failed", "contacts_view_not_settled", "contacts_top_required",
    "contacts_view_not_restored", "contacts_scroll_restore_failed",
    "modifier_or_mouse_button_pressed", "capture_observation_failed",
    "native_capture_active", "invalid_contact_wheel", "stale_process",
    "coordinate_context_unverified", "coordinate_mapping_unverified",
    "wheel_result_unknown", "unverified_geometry", "contact_span_failed",
    "contact_span_evidence_invalid",
    "worker_contact_span_evidence_missing", "contact_span_observation_unverified",
    "contact_span_result_invalid", "contact_span_success_unverified",
    "invalid_contact_span_steps", "invalid_contact_span_limit", "invalid_deadline",
    "contact_span_budget_exhausted", "contact_span_already_started",
    "contact_span_row_invalid",
    "background_side_effect", "monitor_failed",
})

EVIDENCE_KEYS = frozenset({
    "mode", "origin", "requested_steps", "delivery_started", "entered",
    "attempted_down_steps", "completed_steps", "observed_view_count",
    "attempted_restore_steps", "viewport_settled", "contacts_view_restored",
    "conversation_restored", "draft_preserved", "not_full_directory",
    "stable_cursor_supported", "primary_error_code", "cleanup_error_code",
})
_EVIDENCE_BOOL_KEYS = (
    "delivery_started", "entered", "viewport_settled", "contacts_view_restored",
    "conversation_restored", "draft_preserved", "not_full_directory",
    "stable_cursor_supported",
)
_EVIDENCE_INT_RANGES = {
    "requested_steps": (1, 4), "attempted_down_steps": (0, 4),
    "completed_steps": (0, 4), "observed_view_count": (0, 5),
    "attempted_restore_steps": (0, 4),
}
_BODY_KEYS = frozenset({
    "ok", "status", "verification_level", "background_mode", "origin", "views",
    "counts", "not_full_directory", "stable_cursor_supported",
    "original_contacts_view_restored", "original_conversation_restored",
})
_COUNT_KEYS = frozenset({
    "requested_steps", "completed_steps", "view_count", "changed_view_count",
})
_VIEW_KEYS = frozenset({
    "offset", "rows", "exposed_count", "returned_count", "viewport_changed",
})
_ROW_KEYS = frozenset({"display_text"})
_MISSING_EVIDENCE = "worker_contact_span_evidence_missing"
_INVALID_EVIDENCE = "contact_span_evidence_invalid"
_FALLBACK_ERROR = "contact_span_failed"


def _exact_int(value, minimum=None, maximum=None):
    return (type(value) is int
            and (minimum is None or value >= minimum)
            and (maximum is None or value <= maximum))


def safe_contact_span_error(code):
    """Return an allowlisted opaque error code, never arbitrary exception text."""
    if type(code) is str and code in SAFE_CONTACT_SPAN_ERRORS:
        return code
    return _FALLBACK_ERROR


def _error_value(value):
    if value is None:
        return None, True
    if type(value) is str and value in SAFE_CONTACT_SPAN_ERRORS:
        return value, True
    return _FALLBACK_ERROR, False


def normalize_contact_span_evidence(value):
    """Return fixed evidence and whether the original shape was valid."""
    if not isinstance(value, dict):
        return {"primary_error_code": _MISSING_EVIDENCE}, False

    # Preserve only supplied contract fields.  A missing field is not silently
    # materialized as None, while a supplied field with a bad type is retained
    # as None so callers cannot mistake it for an observed value.
    fixed = {}
    shape_valid = set(value) == EVIDENCE_KEYS
    valid = shape_valid
    for key in _EVIDENCE_BOOL_KEYS:
        if key not in value:
            continue
        item = value.get(key)
        if item is None or type(item) is bool:
            fixed[key] = item
        else:
            fixed[key] = None
            shape_valid = valid = False

    for key, expected in (("mode", "bounded_contacts_span"),
                          ("origin", "observed_top")):
        if key not in value:
            continue
        item = value.get(key)
        if item is None or (type(item) is str and item == expected):
            fixed[key] = item
        else:
            fixed[key] = None
            shape_valid = valid = False

    for key, (minimum, maximum) in _EVIDENCE_INT_RANGES.items():
        if key not in value:
            continue
        item = value.get(key)
        if item is None or _exact_int(item, minimum, maximum):
            fixed[key] = item
        else:
            fixed[key] = None
            shape_valid = valid = False

    error_invalid = False
    for key in ("primary_error_code", "cleanup_error_code"):
        if key not in value:
            continue
        fixed[key], error_valid = _error_value(value.get(key))
        valid = valid and error_valid
        error_invalid = error_invalid or not error_valid

    down = fixed.get("attempted_down_steps")
    completed = fixed.get("completed_steps")
    restored = fixed.get("attempted_restore_steps")
    requested = fixed.get("requested_steps")
    observed = fixed.get("observed_view_count")
    entered = fixed.get("entered")
    if (_exact_int(requested, 1, 4) and _exact_int(down, 0, 4)
            and down > requested):
        shape_valid = valid = False
    if (_exact_int(down, 0, 4) and _exact_int(completed, 0, 4)
            and completed > down):
        shape_valid = valid = False
    if (_exact_int(down, 0, 4) and _exact_int(restored, 0, 4)
            and restored > down):
        shape_valid = valid = False
    if (_exact_int(completed, 0, 4) and _exact_int(observed, 0, 5)
            and observed not in (0, completed + 1)):
        shape_valid = valid = False
    if (_exact_int(completed, 1, 4)
            and (entered is not True or observed != completed + 1)):
        shape_valid = valid = False
    if (entered is False and any(
            type(item) is int and item > 0
            for item in (down, completed, restored, observed))):
        shape_valid = valid = False
    if (fixed.get("delivery_started") is False
            and (entered is True or any(
                type(item) is int and item > 0
                for item in (down, completed, restored, observed)))):
        shape_valid = valid = False
    if not valid:
        if shape_valid:
            # An untrusted/NUL-bearing runtime error is represented by the
            # opaque fallback, not mislabeled as an evidence-shape failure.
            fixed["primary_error_code"] = _FALLBACK_ERROR
        elif not error_invalid:
            fixed["primary_error_code"] = _INVALID_EVIDENCE
    return fixed, valid


def _valid_row(value):
    return (isinstance(value, dict) and set(value) == _ROW_KEYS
            and type(value["display_text"]) is str
            and "\x00" not in value["display_text"])


def _valid_body_shape(result, limit_per_view):
    if not isinstance(result, dict) or set(result) != _BODY_KEYS:
        return False
    if (type(result["ok"]) is not bool or result["ok"] is not True
            or result["status"] != "contact_span_observed"
            or result["verification_level"] != "settled_visible_span_with_restoration"
            or result["background_mode"] != "minimized"
            or result["origin"] != "observed_top"
            or result["not_full_directory"] is not True
            or result["stable_cursor_supported"] is not False
            or result["original_contacts_view_restored"] is not True
            or result["original_conversation_restored"] is not True):
        return False

    counts = result["counts"]
    if not isinstance(counts, dict) or set(counts) != _COUNT_KEYS:
        return False
    if not all(_exact_int(counts[key], 0) for key in _COUNT_KEYS):
        return False
    steps = counts["requested_steps"]
    if not _exact_int(steps, 1, 4) or counts["completed_steps"] != steps:
        return False
    if (counts["view_count"] != steps + 1
            or counts["changed_view_count"] > steps):
        return False

    views = result["views"]
    if not isinstance(views, list) or len(views) != steps + 1:
        return False
    changed_count = 0
    for offset, view in enumerate(views):
        if not isinstance(view, dict) or set(view) != _VIEW_KEYS:
            return False
        if (type(view["offset"]) is not int or view["offset"] != offset
                or type(view["exposed_count"]) is not int
                or type(view["returned_count"]) is not int
                or view["exposed_count"] < 0 or view["returned_count"] < 0
                or view["returned_count"] > view["exposed_count"]):
            return False
        if limit_per_view is not None and view["returned_count"] > limit_per_view:
            return False
        rows = view["rows"]
        if (not isinstance(rows, list)
                or len(rows) != view["returned_count"]
                or not all(_valid_row(row) for row in rows)):
            return False
        if type(view["viewport_changed"]) is not bool:
            return False
        if offset == 0 and view["viewport_changed"] is not False:
            return False
        changed_count += int(view["viewport_changed"])
    if changed_count != counts["changed_view_count"]:
        return False
    return True


def valid_contact_span_success(result, evidence, desktop, args):
    """Validate success only when body, evidence, args and desktop gate agree."""
    fixed, valid = normalize_contact_span_evidence(evidence)
    if not valid or not isinstance(desktop, dict) or not isinstance(args, dict):
        return False
    if set(args) != {"steps", "limit_per_view"}:
        return False
    steps, limit = args["steps"], args["limit_per_view"]
    if not _exact_int(steps, 1, 4) or not _exact_int(limit, 1, 100):
        return False
    if desktop.get("background_observation_passed") is not True:
        return False
    if (fixed["mode"] != "bounded_contacts_span"
            or fixed["origin"] != "observed_top"
            or fixed["requested_steps"] != steps
            or fixed["attempted_down_steps"] != steps
            or fixed["completed_steps"] != steps
            or fixed["observed_view_count"] != steps + 1
            or fixed["attempted_restore_steps"] != steps
            or fixed["primary_error_code"] is not None
            or fixed["cleanup_error_code"] is not None
            or any(fixed[key] is not True for key in (
                "delivery_started", "entered", "viewport_settled",
                "contacts_view_restored", "conversation_restored",
                "draft_preserved", "not_full_directory"))
            or fixed["stable_cursor_supported"] is not False):
        return False
    if not _valid_body_shape(result, limit):
        return False
    counts = result["counts"]
    return (counts["requested_steps"] == steps
            and counts["completed_steps"] == fixed["completed_steps"]
            and counts["view_count"] == fixed["observed_view_count"])


def contact_span_journal_summary(result):
    """Return fixed scalar metadata only; reject invalid or body-bearing results."""
    if not _valid_body_shape(result, 100):
        raise ValueError("contact_span_result_invalid")
    return {
        "ok": True,
        "status": result["status"],
        "verification_level": result["verification_level"],
        "background_mode": result["background_mode"],
        "origin": result["origin"],
        "counts": dict(result["counts"]),
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "original_contacts_view_restored": True,
        "original_conversation_restored": True,
    }
