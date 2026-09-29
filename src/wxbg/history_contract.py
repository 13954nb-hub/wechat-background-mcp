"""Fixed, content-free history navigation evidence across process boundaries."""
SAFE_HISTORY_ERRORS = frozenset({
    'invalid_scroll_steps', 'invalid_scroll_direction', 'invalid_session_ref',
    'invalid_deadline', 'history_budget_exhausted', 'history_view_not_settled',
    'history_navigation_failed', 'history_navigation_already_started',
    'context_conflict', 'ambiguous_recipient', 'draft_conflict', 'payment_excluded',
    'history_identity_missing', 'history_geometry_invalid', 'history_frame_ambiguous',
    'history_row_unsupported', 'history_row_identity_invalid', 'history_row_geometry_invalid',
    'history_rows_missing', 'history_rows_overlap', 'history_observation_failed',
    'history_ancestry_invalid', 'history_frame_changed', 'history_row_changed',
    'tree_budget_exceeded', 'stale_process', 'stale_window', 'cannot_disable_auto_focus',
    'background_requires_minimized', 'wechat_has_foreground', 'existing_popup',
    'unverified_layout', 'unverified_geometry', 'modifier_or_mouse_button_pressed',
    'capture_observation_failed', 'native_capture_active', 'coordinate_context_unverified',
    'coordinate_mapping_unverified', 'history_view_changed', 'wheel_result_unknown',
    'history_wheel_observation_failed', 'invalid_history_wheel', 'missing_context_guard',
})


def safe_history_error(code):
    return code if type(code) is str and code in SAFE_HISTORY_ERRORS else 'history_navigation_failed'

_BOOLS = ('delivery_started', 'viewport_settled', 'conversation_preserved',
          'draft_preserved', 'viewport_changed', 'original_viewport_restoration_requested',
          'not_full_history', 'boundary_verified')
_KEYS = frozenset((*_BOOLS, 'mode', 'direction', 'requested_steps',
                   'completed_steps', 'primary_error_code'))


def normalize_history_evidence(value):
    fixed = {key: None for key in _KEYS}
    if not isinstance(value, dict):
        fixed['primary_error_code'] = 'worker_history_evidence_missing'
        return fixed, False
    valid = set(value) == _KEYS
    for key in _BOOLS:
        item = value.get(key)
        if item is None or type(item) is bool:
            fixed[key] = item
        else:
            valid = False
    for key, allowed in (('mode', ('intentional_navigation',)),
                         ('direction', ('older', 'newer'))):
        item = value.get(key)
        if item is None or (type(item) is str and item in allowed):
            fixed[key] = item
        else:
            valid = False
    for key, minimum in (('requested_steps', 1), ('completed_steps', 0)):
        item = value.get(key)
        if item is None or (type(item) is int and minimum <= item <= 4):
            fixed[key] = item
        else:
            valid = False
    code = value.get('primary_error_code')
    if code is None or (type(code) is str and code in SAFE_HISTORY_ERRORS | {'worker_history_evidence_missing', 'history_evidence_invalid'}):
        fixed['primary_error_code'] = code
    else:
        valid = False
    if fixed['delivery_started'] is False and fixed['completed_steps'] not in (None, 0):
        valid = False
        fixed['delivery_started'] = None
    if (type(fixed['requested_steps']) is int and type(fixed['completed_steps']) is int
            and fixed['completed_steps'] > fixed['requested_steps']):
        valid = False
    if not valid:
        fixed['primary_error_code'] = 'history_evidence_invalid'
    return fixed, valid


def valid_history_success(result, evidence, desktop, args):
    value, valid = normalize_history_evidence(evidence)
    if not valid or not isinstance(args, dict):
        return False
    steps = args.get('steps', 1)
    direction = args.get('direction')
    reference = args.get('session_ref')
    if (type(steps) is not int or not 1 <= steps <= 4
            or type(direction) is not str or direction not in ('older', 'newer')
            or type(reference) is not str or not reference):
        return False
    if (value['mode'] != 'intentional_navigation' or value['direction'] != direction
            or value['requested_steps'] != steps or value['completed_steps'] != steps
            or any(value[key] is not True for key in ('delivery_started', 'viewport_settled',
                   'conversation_preserved', 'draft_preserved', 'not_full_history'))
            or type(value['viewport_changed']) is not bool
            or value['original_viewport_restoration_requested'] is not False
            or value['boundary_verified'] is not False or value['primary_error_code'] is not None):
        return False
    if (not isinstance(result, dict)
            or set(result) != {'ok', 'status', 'verification_level', 'background_mode', 'counts', 'refs'}
            or result['ok'] is not True or result['status'] != 'viewport_observed'
            or result['verification_level'] != 'settled_view_after_bounded_scroll'
            or result['background_mode'] != 'minimized'):
        return False
    counts = result['counts']
    if (not isinstance(counts, dict)
            or set(counts) != {'requested_steps', 'completed_steps', 'before_rows', 'after_rows', 'viewport_changed'}
            or any(type(item) is not int for item in counts.values())
            or counts['requested_steps'] != steps or counts['completed_steps'] != steps
            or counts['viewport_changed'] != int(value['viewport_changed'])
            or not 1 <= counts['before_rows'] <= 2500 or not 1 <= counts['after_rows'] <= 2500):
        return False
    return (type(result['refs']) is dict and result['refs'] == {'conversation': reference}
            and isinstance(desktop, dict) and desktop.get('background_observation_passed') is True)
