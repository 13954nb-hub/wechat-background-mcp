"""Bounded Contacts span reads with observed-top and complete restoration."""

from __future__ import annotations

import math
import time

from . import contact_actions as ca
from . import contact_scroll as cs
from .policy import AdapterError


MAX_STEPS = 4
MAX_LIMIT = 100
OUTER_RESPONSE_RESERVE_SECONDS = 1.0
CHAT_CLEANUP_RESERVE_SECONDS = ca.POLL_ATTEMPTS * ca.POLL_SECONDS + 1.0
WHEEL_DELIVERY_RESERVE_SECONDS = 1.0  # contact_wheel.SendMessageTimeoutW timeout
FINAL_SETTLE_RESERVE_SECONDS = 3 * cs.SETTLE_SECONDS
PER_VIEW_RESERVE_SECONDS = WHEEL_DELIVERY_RESERVE_SECONDS + cs.STEP_SECONDS + FINAL_SETTLE_RESERVE_SECONDS

_EVIDENCE_KEYS = {
    "mode", "origin", "requested_steps", "delivery_started", "entered",
    "attempted_down_steps", "completed_steps", "observed_view_count",
    "attempted_restore_steps", "viewport_settled", "contacts_view_restored",
    "conversation_restored", "draft_preserved", "not_full_directory",
    "stable_cursor_supported", "primary_error_code", "cleanup_error_code",
}
_SAFE_CODES = {
    "invalid_contact_span_steps", "invalid_contact_span_limit", "invalid_deadline",
    "contact_span_budget_exhausted", "contact_span_already_started", "contact_span_row_invalid",
    "contact_span_failed", "contacts_top_required", "contacts_view_not_settled",
    "contacts_page_changed", "contacts_not_opened", "contact_changed", "contact_outside_bounds",
    "ambiguous_contact_identity", "contacts_scroll_restore_failed", "contacts_view_not_restored",
    "navigation_restore_failed", "original_chat_not_restored", "context_conflict", "draft_conflict",
    "unverified_layout", "unverified_geometry", "stale_process", "stale_window", "background_requires_minimized",
    "wechat_has_foreground", "existing_popup", "tree_budget_exceeded", "contacts_deadline",
    "contacts_click_failed", "wheel_result_unknown", "invalid_contact_wheel",
    "native_capture_active", "capture_observation_failed", "coordinate_context_unverified",
    "coordinate_mapping_unverified", "modifier_or_mouse_button_pressed",
}


def _evidence(steps):
    return {
        "mode": "bounded_contacts_span",
        "origin": "observed_top",
        "requested_steps": steps if type(steps) is int and 1 <= steps <= MAX_STEPS else None,
        "delivery_started": False,
        "entered": False,
        "attempted_down_steps": 0,
        "completed_steps": 0,
        "observed_view_count": 0,
        "attempted_restore_steps": 0,
        "viewport_settled": None,
        "contacts_view_restored": None,
        "conversation_restored": None,
        "draft_preserved": None,
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "primary_error_code": None,
        "cleanup_error_code": None,
    }


def _safe_code(error):
    code = ca._code(error)
    return code if code in _SAFE_CODES else "contact_span_failed"


def _scroll_reserve(steps):
    return max(FINAL_SETTLE_RESERVE_SECONDS, (2 * steps * PER_VIEW_RESERVE_SECONDS) + FINAL_SETTLE_RESERVE_SECONDS)


def _cleanup_reserve(remaining_steps):
    return (remaining_steps * (WHEEL_DELIVERY_RESERVE_SECONDS + cs.STEP_SECONDS)
            + FINAL_SETTLE_RESERVE_SECONDS)


def _budget(clock, deadline, reserve=0.0):
    try:
        now = clock()
    except Exception:
        raise AdapterError("contact_span_budget_exhausted") from None
    if not isinstance(now, (int, float)) or not math.isfinite(now) or now + reserve >= deadline:
        raise AdapterError("contact_span_budget_exhausted")


def _view_payload(view, offset, limit, changed):
    contacts = view.get("contacts")
    if not isinstance(contacts, list):
        raise AdapterError("contact_span_row_invalid")
    rows = []
    for row in contacts[:limit]:
        display_text = row.get("display_text") if isinstance(row, dict) else None
        if type(display_text) is not str or "\x00" in display_text:
            raise AdapterError("contact_span_row_invalid")
        rows.append({"display_text": display_text})
    return {
        "offset": offset,
        "rows": rows,
        "exposed_count": len(view["contacts"]),
        "returned_count": len(rows),
        "viewport_changed": changed,
    }


def _failure(code, evidence):
    failure = AdapterError(code)
    failure.outcome_unknown = evidence["delivery_started"] is True
    return failure


def collect_contact_span(
    adapter,
    steps,
    limit_per_view,
    deadline,
    *,
    wheel=None,
    clock=time.monotonic,
    sleep=time.sleep,
    phase=None,
):
    """Read origin plus bounded downward destinations, then restore both views."""
    prior_evidence = getattr(adapter, "contact_span_evidence", None)
    evidence = _evidence(steps)
    adapter.contact_span_evidence = evidence
    phase_failed = False

    def mark_phase(name):
        # Diagnostics must never interrupt mandatory restoration. Any recording
        # failure still withholds the result after restoration has been attempted.
        nonlocal phase_failed
        if phase is not None:
            try:
                phase(name)
            except Exception:
                phase_failed = True
    if getattr(adapter, "contact_span_started", False) is True:
        if isinstance(prior_evidence, dict):
            for key in _EVIDENCE_KEYS:
                if key in prior_evidence:
                    evidence[key] = prior_evidence[key]
        evidence["delivery_started"] = True
        evidence["primary_error_code"] = "contact_span_already_started"
        raise _failure("contact_span_already_started", evidence)
    if type(steps) is not int or not 1 <= steps <= MAX_STEPS:
        evidence["primary_error_code"] = "invalid_contact_span_steps"
        raise _failure("invalid_contact_span_steps", evidence)
    if type(limit_per_view) is not int or not 1 <= limit_per_view <= MAX_LIMIT:
        evidence["primary_error_code"] = "invalid_contact_span_limit"
        raise _failure("invalid_contact_span_limit", evidence)
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        evidence["primary_error_code"] = "invalid_deadline"
        raise _failure("invalid_deadline", evidence)
    if wheel is None:
        wheel = cs._wheel
    if not callable(wheel):
        evidence["primary_error_code"] = "contact_span_failed"
        raise _failure("contact_span_failed", evidence)

    scroll_reserve = _scroll_reserve(steps)
    chat_reserve = CHAT_CLEANUP_RESERVE_SECONDS
    try:
        _budget(clock, deadline, OUTER_RESPONSE_RESERVE_SECONDS + chat_reserve + scroll_reserve)
    except Exception as error:
        evidence["primary_error_code"] = _safe_code(error)
        raise _failure(evidence["primary_error_code"], evidence) from None
    read_deadline = deadline - OUTER_RESPONSE_RESERVE_SECONDS - chat_reserve - scroll_reserve
    scroll_cleanup_deadline = deadline - OUTER_RESPONSE_RESERVE_SECONDS - chat_reserve
    chat_cleanup_deadline = deadline - OUTER_RESPONSE_RESERVE_SECONDS
    identity = adapter.identity
    read = ca._Guard(adapter, read_deadline, identity)
    scroll_cleanup = ca._Guard(adapter, scroll_cleanup_deadline, identity)
    chat_cleanup = ca._Guard(adapter, chat_cleanup_deadline, identity)

    navigation = {
        "attempted": False, "entered": False, "restore_attempted": False,
        "restored": False, "primary_error_code": None, "cleanup_error_code": None,
        "scroll": {
            "requested_steps": steps, "attempted_down_steps": 0,
            "attempted_restore_steps": 0, "restored": False,
            "origin": "observed_top", "viewport_changed": False,
            "primary_error_code": None, "cleanup_error_code": None,
        },
    }
    adapter.navigation_evidence = navigation

    primary = None
    cleanup_error = None
    baseline = None
    table_id = None
    wechat = original = None
    tab_attempted = False
    try:
        nodes = read.nodes()
        entry_root = ca._root(read)
        chat = ca._Chat(read, nodes)
        original = chat.expected
        wechat = ca._tab(read, nodes, "微信")
        wechat_rect = ca._tab_rect(read, wechat)
        contacts = ca._tab(read, nodes, "通訊錄")
        contacts_rect = ca._tab_rect(read, contacts)
        if (wechat_rect[0] != contacts_rect[0]
                or wechat_rect[2] != contacts_rect[2]
                or wechat_rect[3] > contacts_rect[1]
                or ca._root(read) != entry_root):
            raise AdapterError("unverified_layout")
        chat.recheck()
        _budget(clock, deadline, OUTER_RESPONSE_RESERVE_SECONDS + chat_reserve + scroll_reserve)
        evidence["delivery_started"] = True
        adapter.contact_span_started = True
        navigation["attempted"] = True
        tab_attempted = True
        mark_phase("enter_contacts")
        adapter.click(contacts)
        for attempt in range(ca.POLL_ATTEMPTS):
            nodes = read.nodes()
            table = ca._contacts_table(nodes)
            if table is not None:
                evidence["entered"] = True
                navigation["entered"] = True
                break
            if attempt + 1 < ca.POLL_ATTEMPTS:
                sleep(ca.POLL_SECONDS)
        else:
            raise AdapterError("contacts_not_opened")

        mark_phase("span_read")
        table_id = ca.runtime_id(table)
        baseline = cs._settled(read, table_id, cs._snapshot(read, table_id, nodes))
        if not baseline["top"]:
            raise AdapterError("contacts_top_required")
        evidence["observed_view_count"] = 1
        views = [_view_payload(baseline, 0, limit_per_view, False)]
        previous = baseline
        for index in range(steps):
            _budget(clock, deadline, OUTER_RESPONSE_RESERVE_SECONDS + chat_reserve
                    + _scroll_reserve(steps - index))
            read.background()
            evidence["attempted_down_steps"] += 1
            navigation["scroll"]["attempted_down_steps"] += 1
            wheel(adapter, baseline["table"], -120)
            sleep(cs.STEP_SECONDS)
            sleep(cs.SETTLE_SECONDS)
            current = cs._settled(read, table_id)
            payload = _view_payload(
                current, index + 1, limit_per_view,
                current["signature"] != previous["signature"],
            )
            evidence["completed_steps"] += 1
            evidence["observed_view_count"] += 1
            views.append(payload)
            previous = current
        evidence["viewport_settled"] = True
        navigation["scroll"]["viewport_changed"] = any(view["viewport_changed"] for view in views[1:])
        result = {
            "ok": True,
            "status": "contact_span_observed",
            "verification_level": "settled_visible_span_with_restoration",
            "background_mode": "minimized",
            "origin": "observed_top",
            "views": views,
            "counts": {
                "requested_steps": steps,
                "completed_steps": evidence["completed_steps"],
                "view_count": len(views),
                "changed_view_count": sum(view["viewport_changed"] for view in views),
            },
            "not_full_directory": True,
            "stable_cursor_supported": False,
            "original_contacts_view_restored": False,
            "original_conversation_restored": False,
        }
    except Exception as error:
        primary = error
        evidence["primary_error_code"] = _safe_code(error)
        navigation["scroll"]["primary_error_code"] = evidence["primary_error_code"]
    finally:
        if evidence["attempted_down_steps"] and baseline is not None and table_id is not None:
            mark_phase("restore_contacts")
            try:
                for _ in range(evidence["attempted_down_steps"]):
                    remaining_steps = (evidence["attempted_down_steps"]
                                       - evidence["attempted_restore_steps"])
                    _budget(clock, deadline, OUTER_RESPONSE_RESERVE_SECONDS + chat_reserve
                            + _cleanup_reserve(remaining_steps))
                    scroll_cleanup.background()
                    evidence["attempted_restore_steps"] += 1
                    navigation["scroll"]["attempted_restore_steps"] += 1
                    wheel(adapter, baseline["table"], 120)
                    sleep(cs.STEP_SECONDS)
                sleep(cs.SETTLE_SECONDS)
                restored = cs._settled(scroll_cleanup, table_id)
                if not restored["top"] or restored["signature"] != baseline["signature"]:
                    raise AdapterError("contacts_view_not_restored")
                evidence["contacts_view_restored"] = True
                navigation["scroll"]["restored"] = True
            except Exception as error:
                cleanup_error = error
                evidence["contacts_view_restored"] = False
                evidence["cleanup_error_code"] = _safe_code(error)
                navigation["scroll"]["cleanup_error_code"] = evidence["cleanup_error_code"]
        if tab_attempted:
            mark_phase("restore_chat")
            try:
                _budget(clock, deadline, OUTER_RESPONSE_RESERVE_SECONDS)
                ca._restore(chat_cleanup, original, wechat, wechat_rect, navigation)
                evidence["conversation_restored"] = True
                evidence["draft_preserved"] = True
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
                evidence["conversation_restored"] = False
                evidence["draft_preserved"] = False
                evidence["cleanup_error_code"] = _safe_code(error)
                navigation["cleanup_error_code"] = evidence["cleanup_error_code"]

    if cleanup_error is not None:
        code = "contacts_scroll_restore_failed" if evidence["attempted_down_steps"] else "navigation_restore_failed"
        raise _failure(code, evidence) from None
    if primary is not None:
        raise _failure(evidence["primary_error_code"], evidence) from None
    if not evidence["contacts_view_restored"] or evidence["conversation_restored"] is not True:
        raise _failure("navigation_restore_failed", evidence) from None
    evidence["contacts_view_restored"] = True
    result["original_contacts_view_restored"] = True
    result["original_conversation_restored"] = True
    if not phase_failed:
        mark_phase("complete")
    if phase_failed:
        evidence["primary_error_code"] = "monitor_failed"
        raise _failure("monitor_failed", evidence) from None
    return result


__all__ = ["collect_contact_span"]
