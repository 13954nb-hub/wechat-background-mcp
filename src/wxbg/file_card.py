"""Read one currently exposed local file card without navigation or mutation."""

from __future__ import annotations

import math
import re
import time

from .file_card_contract import parse_file_card, safe_file_card_error
from .observed_adapter import Observation, runtime_id
from .policy import AdapterError


_REF = re.compile(r"^[0-9a-f]{32}$")
_LOCAL_TIMEOUT = 15.0


def _valid_ref(value):
    return type(value) is str and _REF.fullmatch(value) is not None


def _budget(deadline):
    if time.monotonic() >= deadline:
        raise AdapterError("file_card_budget_exhausted")


def _fresh_observation(adapter, session_ref, expected_session, expected_field):
    adapter.precondition()
    nodes = tuple(adapter.nodes())
    candidates = []
    for node in nodes:
        info = node.element_info
        if info.class_name == "mmui::ChatSessionCell" and adapter.ref(node) == session_ref:
            candidates.append(node)
    if len(candidates) != 1:
        raise AdapterError("context_conflict")
    automation_id = candidates[0].element_info.automation_id
    if type(automation_id) is not str or not automation_id.startswith("session_item_"):
        raise AdapterError("context_conflict")
    title = automation_id[len("session_item_"):]
    if not title:
        raise AdapterError("context_conflict")
    return Observation(
        adapter, nodes, session_ref, title, expected_session, expected_field
    )


def _card_snapshot(node):
    info = node.element_info
    return info.class_name, runtime_id(node), info.automation_id, info.name


def _card(observation, message_ref, expected_message=None):
    matches = []
    candidates = []
    for message in observation.messages:
        if message.kind != "mmui::ChatBubbleItemView":
            continue
        candidates.append(message)
        before = _card_snapshot(message.node)
        if before[:2] != (message.kind, message.runtime) or before[3] != message.name:
            raise AdapterError("file_card_changed")
        current_ref = observation.adapter.ref(message.node, observation.title)
        after = _card_snapshot(message.node)
        if after != before:
            raise AdapterError("file_card_changed")
        if current_ref == message_ref:
            matches.append(message)
    if not matches:
        if expected_message is not None and len([
            message for message in candidates if message.runtime == expected_message.runtime
        ]) == 1:
            raise AdapterError("file_card_changed")
        raise AdapterError("file_card_not_found")
    if len(matches) != 1:
        raise AdapterError("ambiguous_file_card")
    return matches[0]


def _one(adapter, session_ref, message_ref, expected_session=None, expected_field=None,
         expected_message=None):
    observation = _fresh_observation(
        adapter, session_ref, expected_session, expected_field
    )
    message = _card(observation, message_ref, expected_message)
    parsed = parse_file_card(message.name)
    return {
        "observation": observation,
        "message": message,
        "parsed": parsed,
        "draft": observation.draft,
    }


def read_file_card(adapter, session_ref, message_ref, deadline=None):
    """Return fixed local-card metadata from two matching fresh observations."""
    if not _valid_ref(session_ref):
        raise AdapterError("invalid_session_ref")
    if not _valid_ref(message_ref):
        raise AdapterError("invalid_message_ref")
    if deadline is None:
        deadline = time.monotonic() + _LOCAL_TIMEOUT
    elif type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise AdapterError("invalid_deadline")

    try:
        _budget(deadline)
        first = _one(adapter, session_ref, message_ref)
        _budget(deadline)
        second = _one(
            adapter, session_ref, message_ref,
            first["observation"].session_identity,
            first["observation"].field_identity,
            first["message"],
        )
        _budget(deadline)
        if (first["observation"].session_identity != second["observation"].session_identity
                or first["observation"].field_identity != second["observation"].field_identity):
            raise AdapterError("context_conflict")
        if first["draft"] != second["draft"]:
            raise AdapterError("draft_conflict")
        if (first["message"].runtime != second["message"].runtime
                or first["message"].name != second["message"].name
                or first["parsed"] != second["parsed"]):
            raise AdapterError("file_card_changed")
        final_draft = second["observation"].recheck()
        _budget(deadline)
        if final_draft != first["draft"]:
            raise AdapterError("draft_conflict")
        parsed = first["parsed"]
        return {
            "ok": True,
            "status": "observed",
            "verification_level": "two_matching_local_file_card_observations",
            "background_mode": "minimized",
            "transfer_indicator": parsed["transfer_indicator"],
            "progress_percent": parsed["progress_percent"],
            "upload_status": parsed["upload_status"],
            "display_size": parsed["display_size"],
            "remote_receipt_verified": False,
            "stable_message_id": False,
            "not_full_history": True,
            "counts": {"observations": 2},
            "refs": {"conversation": session_ref, "message": message_ref},
        }
    except Exception as error:
        code = getattr(error, "code", "file_card_read_failed")
        code = safe_file_card_error(code)
        raise AdapterError(code) from None


__all__ = ["read_file_card"]
