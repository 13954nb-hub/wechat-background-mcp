"""Version/layout-scoped local file submission; upload status is separate.

The caller owns the operation journal, background guardian, and a verified file
read handle (FILE_SHARE_READ only) throughout this call. This module neither
opens a file nor validates bytes again. native_driver.select_file(descriptor)
owns the one native attachment activation and returns the flat evidence below.
It must revoke its native grant before returning; caller's read handle remains
held through Send and local card verification. No SetValue or automatic retry.
"""
import ntpath
import re
import time

from wxbg.policy import AdapterError
from .observed_adapter import observe, rectangle
from .file_card_contract import parse_file_card

EMBEDDED_FILE = '\ufffc'
POLL_ATTEMPTS = 8
POLL_SECONDS = .15


def _descriptor(fixture):
    if not isinstance(fixture, dict):
        raise AdapterError('invalid_fixture')
    result = {key: fixture.get(key) for key in ('path', 'name', 'size', 'sha256')}
    path, name, size, digest = (result[key] for key in ('path', 'name', 'size', 'sha256'))
    if not isinstance(path, str) or not re.match(r'^[A-Za-z]:\\', path):
        raise AdapterError('invalid_fixture')
    if len(path.encode('utf-16-le')) // 2 >= 260 or any(c in path[2:] for c in ':*?\0\r\n'):
        raise AdapterError('invalid_fixture')
    if not isinstance(name, str) or not name or name != ntpath.basename(path) or any(ord(c) < 32 for c in name):
        raise AdapterError('invalid_fixture')
    if type(size) is not int or not 0 <= size <= 1024 * 1024:
        raise AdapterError('invalid_fixture')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise AdapterError('invalid_fixture')
    return result


def _preflight_point(observation, button, expected):
    """Bind a semantic UIA button to adapter's read-only render geometry gate."""
    value = observation.adapter.preflight_click(button)
    if type(value) is not int or (value & 0xffff, (value >> 16) & 0xffff) != expected:
        raise AdapterError('unverified_layout')


def _file_layout(observation):
    """Measure semantic buttons in the current render client, never scale old points."""
    root = rectangle(observation.adapter.root())
    if (len(root) != 4 or any(type(value) is not int for value in root)):
        raise AdapterError('unverified_layout')
    left, top, right, bottom = root
    width, height = right - left, bottom - top
    if not 640 <= width <= 32767 or not 480 <= height <= 32767:
        raise AdapterError('unverified_layout')
    attachment = observation.one(lambda i: i.control_type == 'Button'
                                 and i.class_name == 'mmui::XButton'
                                 and i.name == '傳送檔案', 'file_attachment')
    send = observation.one(lambda i: i.control_type == 'Button'
                           and i.class_name == 'mmui::XOutlineButton'
                           and i.name == '傳送', 'send_button')
    attachment_rect, send_rect = rectangle(attachment), rectangle(send)
    if (len(attachment_rect) != 4 or len(send_rect) != 4
            or any(type(value) is not int for value in (*attachment_rect, *send_rect))):
        raise AdapterError('unverified_layout')
    ax1, ay1, ax2, ay2 = attachment_rect
    sx1, sy1, sx2, sy2 = send_rect
    attachment_point = ((ax1 + ax2) // 2 - left, (ay1 + ay2) // 2 - top)
    send_point = ((sx1 + sx2) // 2 - left, (sy1 + sy2) // 2 - top)
    if (not 12 <= ax2 - ax1 <= 240 or not 12 <= ay2 - ay1 <= 160
            or not 12 <= sx2 - sx1 <= 300 or not 12 <= sy2 - sy1 <= 200
            or not left + 2 <= ax1 < ax2 <= right - 2
            or not top + 2 <= ay1 < ay2 <= bottom - 2
            or not left + 2 <= sx1 < sx2 <= right - 2
            or not top + 2 <= sy1 < sy2 <= bottom - 2
            or not 16 <= attachment_point[0] <= 3 * width // 4
            or not 3 * height // 4 <= attachment_point[1] < height
            or not width // 2 < send_point[0] < width
            or not height // 2 < send_point[1] < height
            or not attachment_point[0] < send_point[0]):
        raise AdapterError('unverified_layout')
    # Adapter.preflight_click independently verifies the unique render child,
    # exact client dimensions, process identity, and current UIA geometry.
    _preflight_point(observation, attachment, attachment_point)
    _preflight_point(observation, send, send_point)
    return (attachment_rect, send_rect, attachment_point, (width, height))


def _native_evidence(report):
    if not isinstance(report, dict):
        raise AdapterError('native_selection_unverified')
    counts = {'matches': 1, 'accepted_shows': 1, 'live': 0, 'installed': 0,
              'active_filter': 0, 'protection_restored': 1, 'cleanup_unresolved': 0}
    if any(type(report.get(key)) is not int or report[key] != value for key, value in counts.items()):
        raise AdapterError('native_selection_unverified')
    if report.get('released') is not True or report.get('grant_revoked') is not True:
        raise AdapterError('native_cleanup_unverified')
    if report.get('error', 0) != 0 or report.get('passed', True) is not True:
        raise AdapterError('native_selection_unverified')


def _upload_status(name, filename):
    # The parser owns the bounded card grammar and progress/status precedence.
    # A malformed or unsupported card is an observation gap, not a receipt.
    try:
        parsed = parse_file_card(name)
    except Exception:
        return 'unknown'
    if parsed.get('filename') != filename:
        return 'unknown'
    status = parsed.get('upload_status')
    return status if status in ('uploading', 'interrupted', 'failed', 'completed', 'unknown') else 'unknown'


def send_file(adapter, native_driver, session_ref, fixture):
    """Select exactly once, then click semantic Send once, or fail without retry.

    Once submission_started is set, any exception is outcome_unknown. Caller
    must preserve that state in its journal even if no Send click was observed.
    This function never clears a draft or reports a remote delivery receipt.
    """
    if adapter.submission_started:
        raise AdapterError('outcome_unknown', 'an earlier action may have submitted; do not repeat')
    phase = {
        'native_selection_started': False,
        'native_selection_completed': False,
        'embedded_file_staged': False,
        'send_click_attempted': False,
        'local_card_observed': False,
    }
    adapter.file_send_phase = phase
    try:
        descriptor = _descriptor(fixture)
        if not isinstance(session_ref, str) or not session_ref:
            raise AdapterError('invalid_session_ref')
        adapter.precondition()
        opened = adapter.open_session(session_ref)
        title = opened['title']
        initial = observe(adapter, session_ref, title)
        session_identity = initial.session_identity
        field_identity = initial.field_identity

        def current_observation():
            return observe(adapter, session_ref, title, session_identity, field_identity)

        if initial.draft != '':
            raise AdapterError('draft_conflict', 'existing draft was preserved')
        layout = _file_layout(initial)
        prepared = current_observation()
        before = prepared.message_ids
        if _file_layout(prepared) != layout:
            raise AdapterError('unverified_layout')
        # Last-boundary reads follow geometry/provider work; they are direct
        # selection/Value reads over this observation, not another tree walk.
        if prepared.recheck() != '':
            raise AdapterError('draft_conflict', 'draft changed before selection')
        # Other client builds might transmit directly from the native picker.
        # From this boundary onwards, even a timeout cannot justify a retry.
        adapter.submission_started = True
        phase['native_selection_started'] = True
        report = native_driver.select_file(dict(descriptor), attachment_point=layout[2],
                                           attachment_size=layout[3])
        adapter.native_evidence = report
        _native_evidence(report)
        phase['native_selection_completed'] = True

        for attempt in range(POLL_ATTEMPTS):
            selected = current_observation()
            selected_draft = selected.draft
            if selected_draft == EMBEDDED_FILE:
                phase['embedded_file_staged'] = True
            if selected.new_file_cards(before, descriptor['name']):
                phase['local_card_observed'] = True
                raise AdapterError('possible_native_autotransmit', 'do not click Send again')
            if selected_draft == EMBEDDED_FILE:
                break
            if selected_draft != '':
                raise AdapterError('attachment_draft_conflict')
            if attempt + 1 < POLL_ATTEMPTS:
                time.sleep(POLL_SECONDS)
        else:
            raise AdapterError('attachment_draft_unverified')

        presend = current_observation()
        button = presend.one(lambda i: i.control_type == 'Button'
                             and i.class_name == 'mmui::XOutlineButton' and i.name == '傳送', 'send_button')
        if _file_layout(presend) != layout or rectangle(button) != layout[1]:
            raise AdapterError('unverified_layout')
        root_left, root_top, _, _ = rectangle(presend.adapter.root())
        left, top, right, bottom = layout[1]
        _preflight_point(presend, button,
                         ((left + right) // 2 - root_left,
                          (top + bottom) // 2 - root_top))
        if presend.new_file_cards(before, descriptor['name']):
            phase['local_card_observed'] = True
            raise AdapterError('possible_native_autotransmit')
        info = button.element_info
        if info.control_type != 'Button' or info.class_name != 'mmui::XOutlineButton' or info.name != '傳送':
            raise AdapterError('send_button_changed')
        if presend.recheck() != EMBEDDED_FILE:
            raise AdapterError('attachment_draft_conflict')
        phase['send_click_attempted'] = True
        adapter.click(button)

        for attempt in range(POLL_ATTEMPTS):
            submitted = current_observation()
            matches = submitted.new_file_cards(before, descriptor['name'])
            if matches:
                phase['local_card_observed'] = True
            if len(matches) > 1:
                raise AdapterError('ambiguous_attachment_card')
            if len(matches) == 1 and submitted.draft == '':
                # Preserve the final confirmation boundary with a fresh tree.
                # Both observations must agree on the sole new card identity.
                confirmed = current_observation()
                final_matches = confirmed.new_file_cards(before, descriptor['name'])
                if final_matches:
                    phase['local_card_observed'] = True
                if confirmed.draft != '':
                    raise AdapterError('attachment_draft_conflict')
                if len(final_matches) != 1 or final_matches[0].runtime != matches[0].runtime:
                    raise AdapterError('attachment_card_changed')
                message = final_matches[0]
                return {'ok': True, 'status': 'submitted', 'submitted': True,
                        'verification_level': 'new_local_attachment_card_and_empty_draft',
                        'upload_status': _upload_status(message.name, descriptor['name']),
                        'remote_receipt_verified': False, 'counts': {'submitted': 1},
                        'refs': {'message': adapter.ref(message.node, title), 'conversation': session_ref},
                        'background_mode': 'minimized'}
            if attempt + 1 < POLL_ATTEMPTS:
                time.sleep(POLL_SECONDS)
        raise AdapterError('attachment_submission_unverified')
    except Exception as exc:
        evidence = getattr(exc, 'evidence', None)
        if evidence is not None:
            adapter.native_evidence = evidence
        if adapter.submission_started:
            reason = exc.code if isinstance(exc, AdapterError) else type(exc).__name__
            failure = AdapterError('outcome_unknown', f'{reason}; file action started; do not automatically resend')
            if evidence is not None:
                failure.evidence = evidence
            raise failure from exc
        raise
