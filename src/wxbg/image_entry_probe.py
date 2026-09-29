"""Private, bounded PNG image-entry observation.

This module deliberately does not publish an image API.  It reuses the
verified file-selection entry, then reports only bounded local UI evidence.
The caller owns the worker guardian and the operation journal.
"""

from __future__ import annotations

import hashlib
from collections import Counter
import json
import math
import ntpath
from pathlib import Path
import re
import time

from wxbg.policy import AdapterError

from .file_actions import (
    EMBEDDED_FILE,
    _file_layout,
    _native_evidence,
    _preflight_point,
)
from .observed_adapter import observe, rectangle, runtime_id
from .image_row_identity import Reconciler


MAX_FILE_BYTES = 1024 * 1024
LOCAL_TIMEOUT = 15.0
MAX_OBSERVATIONS_PER_PHASE = 4
MAX_NEW_ROWS = 8
SETTLE_SECONDS = 0.15
OWN_TITLE = "檔案傳輸"
ROW_KIND_RE = re.compile(r"^mmui::Chat[A-Za-z0-9_]{0,80}ItemView$")
REF_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SESSION_REF_RE = re.compile(r"^[0-9a-f]{32}$")
DESCRIPTOR_KEYS = frozenset(("path", "name", "size", "sha256"))
IMAGE_LABELS = frozenset(("图片", "圖片", "Image"))
FILE_LABELS = frozenset(("檔案", "文件", "File"))
ROW_KEYS = (
    "kind",
    "ref",
    "runtime_sha256",
    "rectangle",
    "name_length",
    "name_sha256",
    "label_category",
)
PHASES = frozenset((
    "initial", "prepared", "native_select", "native_cleanup",
    "selection_observe", "selection_rows", "selection_draft",
    "presend_observe", "presend_layout", "presend_draft",
    "send_dispatch", "final_observe", "final_rows", "final_draft",
    "complete", "previous_attempt",
))
REASON_CODES = frozenset((
    "outcome_unknown", "invalid_fixture", "invalid_image_file_type",
    "invalid_image_file_size", "invalid_image_sha256", "invalid_session_ref",
    "invalid_deadline", "image_entry_budget_exhausted", "context_conflict",
    "observation_failed", "missing_runtime_id", "not_found", "ambiguous",
    "unverified_layout", "draft_conflict", "image_entry_row_invalid",
    "image_entry_rows_unbounded", "image_entry_observation_unstable",
    "native_selection_unverified", "native_cleanup_unverified",
    "send_button_changed", "background_requires_minimized",
    "wechat_has_foreground", "existing_popup", "tree_budget_exceeded",
    "capture_active", "file_identity_changed", "native_target_unverified",
    "stale_window", "native_binary_unavailable", "image_entry_probe_failed",
    "image_identity_invalid", "image_transition_row_unproved",
))


def _raise(code):
    raise AdapterError(code)


def _validate_session_ref(value):
    if not isinstance(value, str) or not SESSION_REF_RE.fullmatch(value):
        _raise("invalid_session_ref")
    return value


def _validate_deadline(deadline):
    if deadline is None:
        return time.monotonic() + LOCAL_TIMEOUT
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        _raise("invalid_deadline")
    return float(deadline)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        _raise("image_entry_budget_exhausted")


def _settle(deadline):
    """Yield briefly without extending the caller's absolute deadline."""
    _check_deadline(deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise("image_entry_budget_exhausted")
    time.sleep(min(SETTLE_SECONDS, remaining))
    _check_deadline(deadline)


def _valid_local_path(path):
    if (not isinstance(path, str) or not re.match(r"^[A-Za-z]:\\", path)
            or len(path.encode("utf-16-le")) // 2 >= 260
            or "/" in path
            or any(ord(char) < 32 or char in ':*?"<>|' for char in path[2:])
            or any(part in (".", "..") or part.endswith((" ", "."))
                   for part in path[3:].split("\\"))
            or not ntpath.basename(path)):
        _raise("invalid_fixture")
    return path


def _validate_png_descriptor(value):
    """Validate the small descriptor contract without opening the file."""
    if not isinstance(value, dict) or frozenset(value) != DESCRIPTOR_KEYS:
        _raise("invalid_fixture")
    path = _valid_local_path(value.get("path"))
    name = value.get("name")
    if (not isinstance(name, str) or not name
            or name != ntpath.basename(path)
            or any(ord(char) < 32 for char in name)):
        _raise("invalid_fixture")
    if not name.casefold().endswith(".png"):
        _raise("invalid_image_file_type")
    size = value.get("size")
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        _raise("invalid_image_file_size")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        _raise("invalid_image_sha256")
    return {
        "path": path,
        "name": name,
        "size": size,
        "sha256": digest.lower(),
    }


def _session_title(adapter, session_ref, deadline, *, own_only=True):
    """Read the already selected row's title without navigating to it."""
    _check_deadline(deadline)
    try:
        adapter.precondition()
        nodes = tuple(adapter.nodes())
        rows = tuple(
            node for node in nodes
            if node.element_info.class_name == "mmui::ChatSessionCell"
        )
        matches = tuple(node for node in rows if adapter.ref(node) == session_ref)
        if len(matches) != 1:
            _raise("context_conflict")
        node = matches[0]
        automation_id = node.element_info.automation_id
        if (not isinstance(automation_id, str)
                or not automation_id.startswith("session_item_")):
            _raise("context_conflict")
        title = automation_id[len("session_item_"):]
        if (not title or len(title) > 4096
                or (own_only and title != OWN_TITLE)
                or sum(1 for candidate in rows
                       if candidate.element_info.automation_id == automation_id) != 1):
            _raise("context_conflict")
        return title
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("observation_failed") from exc


def _observe_phase(adapter, session_ref, title, expected_session, expected_field,
                   deadline, count):
    if count[0] >= MAX_OBSERVATIONS_PER_PHASE:
        _raise("image_entry_observation_unstable")
    _check_deadline(deadline)
    value = observe(adapter, session_ref, title, expected_session, expected_field)
    count[0] += 1
    _check_deadline(deadline)
    return value


def _bounded_rect(node):
    value = rectangle(node)
    if (type(value) is not tuple or len(value) != 4
            or any(type(number) is not int or not -32768 <= number <= 32767
                   for number in value)
            or value[0] > value[2] or value[1] > value[3]):
        _raise("image_entry_row_invalid")
    return list(value)


def _label_category(name, fixture_name):
    if name in IMAGE_LABELS:
        return "image_label"
    lines = name.splitlines()
    if (len(lines) >= 2 and lines[0] in FILE_LABELS
            and any(line == fixture_name for line in lines[1:])):
        return "owned_file_card"
    return "other"


def _live_message_snapshot(observation, node):
    """Read a row twice around provider-dependent identity/geometry reads."""
    try:
        first_info = node.element_info
        first = (
            first_info.class_name,
            runtime_id(node),
            first_info.name,
            tuple(_bounded_rect(node)),
        )
        ref = observation.adapter.ref(node, observation.title)
        second_info = node.element_info
        second = (
            second_info.class_name,
            runtime_id(node),
            second_info.name,
            tuple(_bounded_rect(node)),
        )
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("image_entry_row_invalid") from exc
    kind, runtime, name, first_rect = first
    if (not isinstance(kind, str) or not ROW_KIND_RE.fullmatch(kind)
            or not isinstance(runtime, str) or not runtime
            or not isinstance(name, str) or len(name) > 4096
            or first != second):
        _raise("image_entry_row_invalid")
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        _raise("image_entry_row_invalid")
    return kind, runtime, name, first_rect, ref


def _row_metadata(observation, before, fixture_name):
    messages = tuple(message for message in observation.messages
                     if message.runtime not in before)
    if not messages or len(messages) > MAX_NEW_ROWS:
        if len(messages) > MAX_NEW_ROWS:
            _raise("image_entry_rows_unbounded")
        return []
    rows = []
    seen_runtime = set()
    seen_ref = set()
    for message in messages:
        first = _live_message_snapshot(observation, message.node)
        second = _live_message_snapshot(observation, message.node)
        kind, runtime, name, first_rect, ref = first
        if (first != second or kind != message.kind or runtime != message.runtime
                or (message.kind == "mmui::ChatBubbleItemView" and message.name != name)):
            _raise("image_entry_row_invalid")
        if runtime in seen_runtime:
            _raise("image_entry_row_invalid")
        seen_runtime.add(runtime)
        if ref in seen_ref:
            _raise("image_entry_row_invalid")
        seen_ref.add(ref)
        rows.append({
            "kind": kind,
            "ref": ref,
            "runtime_sha256": hashlib.sha256(runtime.encode("utf-8")).hexdigest(),
            "rectangle": list(first_rect),
            "name_length": len(name),
            "name_sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
            "label_category": _label_category(name, fixture_name),
        })
    rows.sort(key=lambda row: (row["runtime_sha256"], row["kind"], row["ref"]))
    return rows


def _row_signature(rows):
    return tuple(tuple(row[key] if key != "rectangle" else tuple(row[key])
                         for key in ROW_KEYS) for row in rows)


def _identity_rows(observation, deadline):
    if len(observation.messages) > 256:
        _raise('image_identity_invalid')
    rows = []
    for message in observation.messages:
        _check_deadline(deadline)
        first = _live_message_snapshot(observation, message.node)
        second = _live_message_snapshot(observation, message.node)
        if first != second or first[0] != message.kind or first[1] != message.runtime:
            _raise('image_identity_invalid')
        kind, runtime, name, rect, ref = first
        rows.append({'kind':kind,'runtime':runtime,'name':name,'rectangle':rect,'ref':ref})
    _check_deadline(deadline)
    return rows


def _trace(adapter, stage, summary, draft, rows, rows_seen, embedded, stable,
           *, diagnostics=None):
    frames = adapter.image_evidence[stage + '_frames']
    if len(frames) >= MAX_OBSERVATIONS_PER_PHASE:
        _raise('image_identity_invalid')
    frame = {'index':len(frames)+1,
        'draft_kind':'empty' if draft == '' else 'embedded' if draft == EMBEDDED_FILE else 'other',
        **summary, 'new_rows_count':len(rows),'rows_seen':rows_seen,
        'embedded_observations':embedded,'stable_count':stable}
    if stage == 'selection':
        frame['diagnostics'] = diagnostics
    frames.append(frame)


def _draft_kind(value):
    return 'empty' if value == '' else 'embedded' if value == EMBEDDED_FILE else 'other'


def _selection_diagnostics(baseline_rows, current_rows, initial_draft,
                           first_recheck_draft, rows, decision):
    """Only bounded booleans and counts; never publish row text or geometry."""
    same_count = len(baseline_rows) == len(current_rows)
    pairs = zip(baseline_rows, current_rows)
    same_content = same_count and all(
        (before['kind'], before['name']) == (after['kind'], after['name'])
        for before, after in pairs)
    same_geometry = same_count and all(
        before['rectangle'] == after['rectangle']
        for before, after in zip(baseline_rows, current_rows))
    same_runtime = same_count and all(
        before['runtime'] == after['runtime']
        for before, after in zip(baseline_rows, current_rows))
    kind_counts = {'text':0,'bubble':0,'bubble_refer':0,'separator':0,'other':0}
    label_counts = {'image_label':0,'owned_file_card':0,'other':0}
    kinds = {
        'mmui::ChatTextItemView':'text',
        'mmui::ChatBubbleItemView':'bubble',
        'mmui::ChatBubbleReferItemView':'bubble_refer',
        'mmui::ChatItemView':'separator',
    }
    for row in rows:
        kind_counts[kinds.get(row['kind'],'other')] += 1
        label_counts[row['label_category']] += 1
    return {
        'draft_initial_kind':_draft_kind(initial_draft),
        'draft_first_recheck_kind':_draft_kind(first_recheck_draft),
        'scene_rebind_enabled':initial_draft == first_recheck_draft == EMBEDDED_FILE,
        'same_scene_content':same_content,
        'same_scene_geometry':same_geometry,
        'same_scene_runtime':same_runtime,
        'new_row_kind_counts':kind_counts,
        'new_row_label_counts':label_counts,
        'decision':decision,
    }


def _trace_selection(adapter, summary, selected, first_recheck_draft,
                     observed_draft, baseline_rows, current_rows, rows,
                     rows_seen, embedded, stable, decision):
    diagnostics = _selection_diagnostics(
        baseline_rows, current_rows, selected.draft, first_recheck_draft,
        rows, decision)
    _trace(adapter, 'selection', summary, observed_draft, rows, rows_seen,
           embedded, stable, diagnostics=diagnostics)


def _image_counts(rows, fixture_name):
    counts = Counter()
    for row in rows:
        category = _label_category(row['name'],fixture_name)
        if (category == 'image_label' and row['kind'] in ('mmui::ChatBubbleItemView','mmui::ChatBubbleReferItemView')
                or category == 'owned_file_card' and row['kind'] == 'mmui::ChatBubbleItemView'):
            counts[(category,len(row['name']),hashlib.sha256(row['name'].encode('utf-8')).hexdigest())] += 1
    return counts


def _novel_image(rows, fixture_name, before_counts):
    current = _image_counts(rows,fixture_name)
    return any(count > before_counts[key] and (key[0] == 'image_label' or before_counts[key] == 0)
               for key,count in current.items())


def _appended_image_after_tail(baseline, current, fixture_name, ignored_runtime_ids, ignored_refs=()):
    """Local post-Send evidence when earlier rows move outside the viewport."""
    if not baseline or not current:
        return False
    anchor = baseline[-1]
    bubbles = ('mmui::ChatBubbleItemView','mmui::ChatBubbleReferItemView')
    if anchor['kind'] not in bubbles:
        return False
    keys = ('kind','runtime','ref','name')
    matches = [index for index,row in enumerate(current)
               if all(row[key] == anchor[key] for key in keys)]
    if len(matches) != 1:
        return False
    tail = current[matches[0]+1:]
    if not (len(tail) == 1 or len(tail) == 2 and tail[0]['kind'] == 'mmui::ChatItemView'):
        return False
    if len(tail) == 2 and re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',tail[0]['name']) is None:
        return False
    chain = [current[matches[0]]] + tail
    if any(row['rectangle'][0] >= row['rectangle'][2] or row['rectangle'][1] >= row['rectangle'][3]
           for row in chain):
        return False
    for upper,lower in zip(chain,chain[1:]):
        a,b = upper['rectangle'],lower['rectangle']
        if a[3] > b[1] or max(a[0],b[0]) >= min(a[2],b[2]):
            return False
    image = tail[-1]
    if (image['runtime'] in ignored_runtime_ids or image['ref'] in ignored_refs
            or any(image['runtime'] == row['runtime'] or image['ref'] == row['ref'] for row in baseline)):
        return False
    category = _label_category(image['name'],fixture_name)
    if category == 'image_label':
        return image['kind'] in bubbles
    if category == 'owned_file_card' and image['kind'] == 'mmui::ChatBubbleItemView':
        return not any(_label_category(row['name'],fixture_name) == 'owned_file_card' for row in baseline)
    return False


def _result(status, rows, send_clicks, image_novelty):
    if not image_novelty or not any(row['label_category'] in ('image_label','owned_file_card') for row in rows):
        _raise('image_transition_row_unproved')
    return {
        "status": status,
        "diagnostic_only": True,
        "remote_receipt_verified": False,
        "counts": {
            "native_selections": 1,
            "send_clicks": send_clicks,
            "new_rows": len(rows),
        },
        "rows": rows,
    }


def _strict_native_evidence(report):
    """Require the driver's complete terminal cleanup contract locally."""
    _native_evidence(report)
    if (report.get("passed") is not True
            or type(report.get("shows")) is not int or report.get("shows") != 0
            or report.get("hook_removed") is not True
            or report.get("local_module_released") is not True
            or report.get("remote_module_present") is not True
            or report.get("primary_error") is not None
            or report.get("cleanup_errors") != []
            or report.get("submission_started") is not True):
        _raise("native_cleanup_unverified")


def _unknown(adapter, *, phase, reason_code, native_selections, send_calls,
             evidence=None):
    if evidence is None:
        evidence = getattr(adapter, "native_evidence", None)
    if evidence is not None:
        adapter.native_evidence = evidence
    safe_phase = phase if isinstance(phase, str) and phase in PHASES else "initial"
    safe_reason = (reason_code if isinstance(reason_code, str)
                   and reason_code in REASON_CODES else "unexpected_probe_error")
    safe_native = 1 if type(native_selections) is int and native_selections == 1 else 0
    safe_send = 1 if type(send_calls) is int and send_calls == 1 else 0
    detail = json.dumps({
        "phase": safe_phase,
        "reason_code": safe_reason,
        "native_selections": safe_native,
        "send_calls": safe_send,
    }, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    failure = AdapterError(
        "outcome_unknown",
        detail,
    )
    failure.outcome_unknown = True
    failure.detail = detail
    if evidence is not None:
        failure.evidence = evidence
    return failure


def _send_button(observation, deadline, layout):
    _check_deadline(deadline)
    button = observation.one(
        lambda info: info.control_type == "Button"
        and info.class_name == "mmui::XOutlineButton"
        and info.name == "傳送",
        "send_button",
    )
    if rectangle(button) != layout[1]:
        _raise("unverified_layout")
    info = button.element_info
    if (info.control_type != "Button"
            or info.class_name != "mmui::XOutlineButton"
            or info.name != "傳送"
            or not runtime_id(button)):
        _raise("send_button_changed")
    root_left, root_top, _, _ = rectangle(observation.adapter.root())
    left, top, right, bottom = layout[1]
    _preflight_point(observation, button,
                     ((left + right) // 2 - root_left,
                      (top + bottom) // 2 - root_top))
    return button


def _record_phase(adapter, value):
    callback = getattr(adapter, "image_phase", None)
    if callback is not None:
        callback(value)


def probe(adapter, driver, session_ref, file, deadline=None, *, own_only=True):
    """Run the private state machine against an already selected session."""
    descriptor = _validate_png_descriptor(file)
    session_ref = _validate_session_ref(session_ref)
    deadline = _validate_deadline(deadline)
    if getattr(adapter, "submission_started", False):
        raise _unknown(
            adapter,
            phase="previous_attempt",
            reason_code="outcome_unknown",
            native_selections=0,
            send_calls=0,
        )

    phase = "initial"
    native_selections = 0
    send_calls = 0
    adapter.image_evidence = {'version':1,'baseline':None,'selection_frames':[],'final_frames':[]}
    try:
        _record_phase(adapter, "image_preflight")
        phase = "initial"
        title = _session_title(adapter, session_ref, deadline, own_only=own_only)
        initial = _observe_phase(
            adapter, session_ref, title, None, None, deadline, [0]
        )
        session_identity = initial.session_identity
        field_identity = initial.field_identity
        if initial.draft != "":
            _raise("draft_conflict")
        layout = _file_layout(initial)

        phase = "prepared"
        setup_count = [0]
        prepared = _observe_phase(
            adapter, session_ref, title, session_identity, field_identity,
            deadline, setup_count,
        )
        if _file_layout(prepared) != layout:
            _raise("unverified_layout")
        _check_deadline(deadline)
        if prepared.draft != "" or prepared.recheck() != "":
            _raise("draft_conflict")
        prepared_rows = _identity_rows(prepared, deadline)
        identity = Reconciler(prepared_rows, descriptor['name'])
        before_image_counts = _image_counts(prepared_rows, descriptor['name'])
        adapter.image_evidence['baseline'] = identity.baseline_summary
        if prepared.recheck() != '':
            _raise('draft_conflict')

        phase = "native_select"
        _record_phase(adapter, "image_native_select")
        _check_deadline(deadline)
        adapter.submission_started = True
        native_selections = 1
        report = driver.select_file(dict(descriptor), attachment_point=layout[2],
                                    attachment_size=layout[3])
        adapter.native_evidence = report
        phase = "native_cleanup"
        _strict_native_evidence(report)
        _check_deadline(deadline)
        _record_phase(adapter, "image_selection_observe")

        selection_count = [0]
        embedded_observations = 0
        rows_seen = False
        stable_rows = None
        stable_count = 0
        while selection_count[0] < MAX_OBSERVATIONS_PER_PHASE:
            phase = "selection_observe"
            if selection_count[0] > 0:
                _settle(deadline)
            phase = "selection_observe"
            selected = _observe_phase(
                adapter, session_ref, title, session_identity, field_identity,
                deadline, selection_count,
            )
            phase = "selection_rows"
            scene_draft = selected.recheck()
            selected_rows = _identity_rows(selected, deadline)
            identity_summary = identity.reconcile(selected_rows,
                allow_scene_rebind=selected.draft == scene_draft == EMBEDDED_FILE)
            identity_summary['image_novelty'] = _novel_image(selected_rows, descriptor['name'], before_image_counts)
            rows = _row_metadata(selected, identity.ignored_runtime_ids, descriptor["name"])
            phase = "selection_draft"
            _check_deadline(deadline)
            observed_draft = selected.recheck()
            if observed_draft != scene_draft:
                _trace_selection(adapter, identity_summary, selected,
                    scene_draft, observed_draft, prepared_rows, selected_rows,
                    rows, rows_seen, embedded_observations, stable_count,
                    'draft_conflict')
                _raise('draft_conflict')
            _check_deadline(deadline)
            if rows:
                rows_seen = True
                embedded_observations = 0
                signature = (_row_signature(rows), tuple(sorted(_image_counts(selected_rows, descriptor['name']).items())))
                if signature == stable_rows and observed_draft == "":
                    stable_count += 1
                else:
                    stable_rows = signature
                    stable_count = 1 if observed_draft == "" else 0
                decision = ('automatic_transition' if observed_draft == "" and stable_count >= 2
                    else 'max_frames_exhausted' if selection_count[0] >= MAX_OBSERVATIONS_PER_PHASE
                    else 'continue')
                _trace_selection(adapter, identity_summary, selected,
                    scene_draft, observed_draft, prepared_rows, selected_rows,
                    rows, rows_seen, embedded_observations, stable_count,
                    decision)
                if observed_draft == "" and stable_count >= 2:
                    _check_deadline(deadline)
                    if selected.recheck() != "":
                        _raise("draft_conflict")
                    _check_deadline(deadline)
                    phase = "complete"
                    _record_phase(adapter, "complete")
                    return _result("automatic_transition_observed", rows, 0,
                        _novel_image(selected_rows, descriptor['name'], before_image_counts))
                continue

            stable_rows = None
            stable_count = 0
            if observed_draft == EMBEDDED_FILE:
                embedded_observations += 1
                decision = ('sticky_rows_seen' if embedded_observations >= 2 and rows_seen
                    else 'presend' if embedded_observations >= 2
                    else 'max_frames_exhausted' if selection_count[0] >= MAX_OBSERVATIONS_PER_PHASE
                    else 'continue')
                _trace_selection(adapter, identity_summary, selected,
                    scene_draft, observed_draft, prepared_rows, selected_rows,
                    rows, rows_seen, embedded_observations, stable_count,
                    decision)
                if embedded_observations >= 2:
                    break
                continue
            if observed_draft != "":
                _trace_selection(adapter, identity_summary, selected,
                    scene_draft, observed_draft, prepared_rows, selected_rows,
                    rows, rows_seen, embedded_observations, stable_count,
                    'draft_conflict')
                _raise("draft_conflict")
            embedded_observations = 0
            decision = ('max_frames_exhausted' if selection_count[0] >= MAX_OBSERVATIONS_PER_PHASE
                        else 'continue')
            _trace_selection(adapter, identity_summary, selected,
                scene_draft, observed_draft, prepared_rows, selected_rows,
                rows, rows_seen, embedded_observations, stable_count,
                decision)

        else:
            _raise("image_entry_observation_unstable")

        if rows_seen:
            _raise("image_entry_observation_unstable")

        phase = "presend_observe"
        _record_phase(adapter, "image_presend")
        presend_count = [0]
        presend = _observe_phase(
            adapter, session_ref, title, session_identity, field_identity,
            deadline, presend_count,
        )
        phase = "presend_draft"
        if presend.draft != EMBEDDED_FILE:
            _raise("outcome_unknown")
        phase = "presend_layout"
        presend_rows = _identity_rows(presend, deadline)
        identity.reconcile(presend_rows, allow_scene_rebind=True)
        if _row_metadata(presend, identity.ignored_runtime_ids, descriptor["name"]):
            _raise("outcome_unknown")
        _check_deadline(deadline)
        phase = "presend_draft"
        if presend.recheck() != EMBEDDED_FILE:
            _raise("draft_conflict")
        _check_deadline(deadline)
        phase = "presend_layout"
        if _file_layout(presend) != layout:
            _raise("unverified_layout")
        button = _send_button(presend, deadline, layout)
        _check_deadline(deadline)
        phase = "presend_draft"
        if presend.recheck() != EMBEDDED_FILE:
            _raise("draft_conflict")
        _check_deadline(deadline)
        phase = "send_dispatch"
        _record_phase(adapter, "image_send_dispatch")
        _check_deadline(deadline)
        protected_runtime_ids = identity.ignored_runtime_ids | frozenset(row['runtime'] for row in presend_rows)
        protected_refs = identity.ignored_refs | frozenset(row['ref'] for row in presend_rows)
        send_calls = 1
        adapter.click(button)
        _record_phase(adapter, "image_final_observe")

        final_count = [0]
        stable_rows = None
        stable_count = 0
        while final_count[0] < MAX_OBSERVATIONS_PER_PHASE:
            phase = "final_observe"
            _settle(deadline)
            phase = "final_observe"
            submitted = _observe_phase(
                adapter, session_ref, title, session_identity, field_identity,
                deadline, final_count,
            )
            phase = "final_rows"
            try:
                submitted_rows = _identity_rows(submitted, deadline)
                identity_summary = identity.reconcile(submitted_rows, allow_separator_updates=True)
                final_image_novelty = (_novel_image(submitted_rows, descriptor['name'], before_image_counts)
                    or _appended_image_after_tail(presend_rows, submitted_rows, descriptor['name'],
                        protected_runtime_ids | identity.ignored_runtime_ids, protected_refs | identity.ignored_refs))
                identity_summary['image_novelty'] = final_image_novelty
                rows = _row_metadata(submitted, identity.ignored_runtime_ids, descriptor["name"])
            except AdapterError as error:
                if error.code not in ('image_identity_invalid','image_entry_row_invalid'):
                    raise
                # Qt may rebuild rows during Send's layout refresh. Only repeat
                # reads; never repeat the selection or Send above this loop.
                stable_rows = None
                stable_count = 0
                continue
            phase = "final_draft"
            _check_deadline(deadline)
            observed_draft = submitted.recheck()
            _check_deadline(deadline)
            if observed_draft != "":
                stable_rows = None
                stable_count = 0
                _trace(adapter,'final',identity_summary,observed_draft,rows,rows_seen,0,stable_count)
                continue
            if not rows:
                stable_rows = None
                stable_count = 0
                _trace(adapter,'final',identity_summary,observed_draft,rows,rows_seen,0,stable_count)
                continue
            signature = (_row_signature(rows), tuple(sorted(_image_counts(submitted_rows, descriptor['name']).items())),
                         identity_summary['row_count'], identity_summary['runtime_sha256'], identity_summary['semantic_sha256'])
            if signature == stable_rows:
                stable_count += 1
            else:
                stable_rows = signature
                stable_count = 1
            _trace(adapter,'final',identity_summary,observed_draft,rows,True,0,stable_count)
            if stable_count >= 2:
                _check_deadline(deadline)
                if submitted.recheck() != "":
                    _raise("draft_conflict")
                _check_deadline(deadline)
                phase = "complete"
                _record_phase(adapter, "complete")
                return _result("local_transition_observed", rows, 1,
                    final_image_novelty)
        _raise("image_entry_observation_unstable")
    except AdapterError as exc:
        if getattr(adapter, "submission_started", False):
            evidence = getattr(exc, "evidence", None)
            raise _unknown(
                adapter,
                phase=phase,
                reason_code=getattr(exc, "code", None),
                native_selections=native_selections,
                send_calls=send_calls,
                evidence=evidence,
            ) from exc
        raise
    except Exception as exc:
        if getattr(adapter, "submission_started", False):
            raise _unknown(
                adapter,
                phase=phase,
                reason_code="unexpected_probe_error",
                native_selections=native_selections,
                send_calls=send_calls,
                evidence=getattr(exc, "evidence", None),
            ) from exc
        raise AdapterError("image_entry_probe_failed") from exc


def run(adapter, session_ref, file, deadline=None, *, own_only=False):
    """Hold a VerifiedFile while constructing the pinned native driver."""
    session_ref = _validate_session_ref(session_ref)
    requested = _validate_png_descriptor(file)
    path = requested["path"]
    expected_sha256 = requested["sha256"]
    expected_size = requested["size"]

    from .attachments import NATIVE_SHA256
    from .native_driver import NativeAttachmentDriver, VerifiedFile
    import win32process

    with VerifiedFile(
            path,
            expected_sha256=expected_sha256,
            expected_size=expected_size) as verified:
        descriptor = _validate_png_descriptor(verified)
        if (descriptor["name"] != requested["name"]
                or descriptor["size"] != requested["size"]
                or descriptor["sha256"] != requested["sha256"]):
            _raise("file_identity_changed")
        try:
            tid, pid = win32process.GetWindowThreadProcessId(adapter.hwnd)
        except Exception as exc:
            raise AdapterError("native_target_unverified") from exc
        if (type(tid) is not int or type(pid) is not int
                or pid != adapter.pid):
            _raise("stale_window")
        target = {
            "pid": pid,
            "hwnd": adapter.hwnd,
            "created": adapter.created,
            "tid": tid,
        }
        try:
            dll_path = (
                Path(__file__).resolve().parent
                / "native" / NATIVE_SHA256 / "wxbg_attachment.dll"
            ).resolve(strict=True)
        except Exception as exc:
            raise AdapterError("native_binary_unavailable") from exc
        driver = NativeAttachmentDriver(target, dll_path, NATIVE_SHA256)
        return probe(adapter, driver, session_ref, descriptor, deadline, own_only=own_only)
