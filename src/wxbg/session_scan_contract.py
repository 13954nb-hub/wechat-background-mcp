"""Typed, content-free evidence for bounded Chats-list navigation."""

SAFE_SCAN_ERRORS = frozenset({
    'TARGET_NOT_VISIBLE', 'TARGET_AMBIGUOUS',
    'invalid_session_scan', 'invalid_title', 'invalid_max_steps',
    'invalid_deadline', 'invalid_scroll_steps', 'invalid_scan_direction',
    'payment_excluded',
    'not_found', 'ambiguous', 'invalid_session_ref', 'session_not_opened',
    'session_scan_failed', 'session_scan_budget_exhausted', 'session_scan_deadline',
    'session_scan_view_changed', 'session_scan_not_settled',
    'session_table_unverified', 'session_row_unverified',
    'session_row_outside_table', 'session_view_empty', 'session_view_changed',
    'session_view_overflow',
    'invalid_session_wheel',
    'session_scan_geometry_unverified', 'session_scan_row_ambiguous',
    'session_scan_target_ambiguous', 'session_scan_selection_unverified',
    'session_target_partially_clipped',
    'session_scan_end_unverified', 'session_scan_wheel_failed',
    'wheel_result_unknown', 'context_conflict', 'ambiguous_recipient',
    'draft_conflict', 'tree_budget_exceeded', 'stale_process', 'stale_window',
    'cannot_disable_auto_focus', 'background_requires_minimized',
    'wechat_has_foreground', 'existing_popup', 'background_side_effect',
    'unverified_layout',
    'unverified_geometry', 'modifier_or_mouse_button_pressed',
    'capture_observation_failed', 'native_capture_active',
    'coordinate_context_unverified', 'coordinate_mapping_unverified',
    'offscreen_control', 'popup_action_unavailable', 'native_parent_missing',
})

_BOOLS = ('delivery_started', 'activation_started', 'target_found',
          'selected_and_header_verified', 'reached_end')
_COUNTS = ('requested_steps', 'attempted_steps', 'delivered_steps')
_KEYS = frozenset((*_BOOLS, *_COUNTS, 'direction', 'primary_error_code'))


def safe_scan_error(code):
    return code if type(code) is str and code in SAFE_SCAN_ERRORS else 'session_scan_failed'


def normalize_scan_evidence(value):
    fixed = {key: None for key in _KEYS}
    if not isinstance(value, dict):
        fixed['primary_error_code'] = 'worker_scan_evidence_missing'
        return fixed, False
    valid = set(value) == _KEYS
    for key in _BOOLS:
        item = value.get(key)
        if type(item) is bool:
            fixed[key] = item
        else:
            valid = False
    for key in _COUNTS:
        item = value.get(key)
        if type(item) is int and 0 <= item <= 4:
            fixed[key] = item
        else:
            valid = False
    direction = value.get('direction')
    if type(direction) is str and direction in ('down', 'up'):
        fixed['direction'] = direction
    else:
        valid = False
    code = value.get('primary_error_code')
    if code is None:
        fixed['primary_error_code'] = None
    elif type(code) is str and code in SAFE_SCAN_ERRORS:
        fixed['primary_error_code'] = code
    else:
        fixed['primary_error_code'] = 'session_scan_failed'
        valid = False
    requested, attempted, delivered = (fixed[key] for key in _COUNTS)
    if (type(requested) is int and type(attempted) is int and type(delivered) is int
            and not 0 <= delivered <= attempted <= requested <= 4):
        valid = False
    if (type(attempted) is int and type(fixed['delivery_started']) is bool
            and fixed['delivery_started'] is not (attempted > 0)):
        valid = False
    if fixed['selected_and_header_verified'] is True and fixed['target_found'] is not True:
        valid = False
    if fixed['activation_started'] is True and fixed['target_found'] is not True:
        valid = False
    if fixed['reached_end'] is True and fixed['target_found'] is True:
        valid = False
    if not valid:
        fixed['primary_error_code'] = 'session_scan_evidence_invalid'
    return fixed, valid


def _valid_background(evidence):
    return (isinstance(evidence, dict)
            and evidence.get('scan_background_observation_passed') is True
            and evidence.get('target_foreground_observed') is False
            and type(evidence.get('observations')) is int
            and evidence['observations'] >= 1
            and type(evidence.get('foreground_changed')) is bool
            and evidence.get('cursor_changed') is False
            and evidence.get('clipboard_changed') is False
            and evidence.get('target_restored') is False
            and evidence.get('capture_observed') is False
            and evidence.get('new_visible_windows') == []
            and evidence.get('monitor_errors') == [])


def valid_scan_success(result, evidence, desktop, args):
    value, valid = normalize_scan_evidence(evidence)
    if not valid or not isinstance(args, dict) or not _valid_background(desktop):
        return False
    title, steps, direction = (args.get('title'), args.get('max_steps', 4),
                               args.get('direction', 'down'))
    if (type(title) is not str or not title or len(title) > 256
            or type(steps) is not int or not 0 <= steps <= 4
            or type(direction) is not str or direction not in ('down', 'up')
            or value['direction'] != direction
            or value['requested_steps'] != steps
            or value['attempted_steps'] != value['delivered_steps']
            or value['primary_error_code'] is not None):
        return False
    if (not isinstance(result, dict)
            or set(result) != {'ok', 'status', 'title', 'verification_level',
                               'counts', 'background_mode'}
            or result['ok'] is not True
            or result['background_mode'] != 'minimized'):
        return False
    counts = result['counts']
    if (not isinstance(counts, dict) or set(counts) != {'wheel_steps', 'viewports'}
            or type(counts['wheel_steps']) is not int
            or type(counts['viewports']) is not int
            or counts['wheel_steps'] != value['delivered_steps']
            or counts['viewports'] != value['delivered_steps'] + 1):
        return False
    status = result['status']
    if status == 'opened':
        return (result['title'] == title
                and result['verification_level'] == 'selected_row_and_header'
                and value['target_found'] is True
                and value['selected_and_header_verified'] is True
                and value['reached_end'] is False)
    if status == 'target_partially_visible':
        return (result['title'] is None
                and result['verification_level'] == 'bounded_viewport_observation'
                and value['target_found'] is True
                and value['selected_and_header_verified'] is False
                and value['activation_started'] is False
                and value['reached_end'] is False)
    if status in ('not_found_in_bounded_scan', 'viewport_unchanged'):
        return (result['title'] is None
                and result['verification_level'] == 'bounded_viewport_observation'
                and value['target_found'] is False
                and value['selected_and_header_verified'] is False
                and value['activation_started'] is False
                and value['reached_end'] is False)
    return False
