"""Fail-closed public result validation for bounded Chats viewport enumeration."""

from .session_scan_contract import _valid_background, normalize_scan_evidence


_RESULT_KEYS = frozenset({
    'ok', 'status', 'viewports', 'counts', 'coverage', 'end_verified',
    'background_mode',
})
_COUNT_KEYS = frozenset({'wheel_steps', 'viewports'})
_VIEW_KEYS = frozenset({'index', 'rows'})
_ROW_KEYS = frozenset({'title', 'fully_visible'})


def valid_session_viewports_success(result, evidence, desktop, args):
    """Accept only observed rows and verified bounded navigation; no full-list claim."""
    scan, valid = normalize_scan_evidence(evidence)
    if not valid or not _valid_background(desktop) or type(args) is not dict:
        return False
    steps = args.get('max_steps', 4)
    direction = args.get('direction', 'down')
    if (type(steps) is not int or not 0 <= steps <= 4
            or type(direction) is not str or direction not in ('down', 'up')
            or scan['requested_steps'] != steps
            or scan['direction'] != direction
            or scan['attempted_steps'] != scan['delivered_steps']
            or scan['primary_error_code'] is not None
            or any(scan[key] is not False for key in (
                'activation_started', 'target_found',
                'selected_and_header_verified', 'reached_end'))):
        return False
    if (type(result) is not dict or set(result) != _RESULT_KEYS
            or result['ok'] is not True
            or result['status'] not in ('bounded_complete', 'viewport_unchanged')
            or result['coverage'] != 'bounded_ui_viewports'
            or result['end_verified'] is not False
            or result['background_mode'] != 'minimized'):
        return False
    delivered = scan['delivered_steps']
    if (result['status'] == 'bounded_complete' and delivered != steps
            or result['status'] == 'viewport_unchanged'
            and not 1 <= delivered <= steps):
        return False
    counts = result['counts']
    viewports = result['viewports']
    if (type(counts) is not dict or set(counts) != _COUNT_KEYS
            or type(counts['wheel_steps']) is not int
            or type(counts['viewports']) is not int
            or counts != {'wheel_steps': delivered, 'viewports': delivered + 1}
            or type(viewports) is not list
            or len(viewports) != delivered + 1):
        return False
    for index, viewport in enumerate(viewports):
        if (type(viewport) is not dict or set(viewport) != _VIEW_KEYS
                or type(viewport['index']) is not int
                or viewport['index'] != index
                or type(viewport['rows']) is not list
                or not 1 <= len(viewport['rows']) <= 32):
            return False
        for row in viewport['rows']:
            if (type(row) is not dict or set(row) != _ROW_KEYS
                    or type(row['title']) is not str
                    or not 1 <= len(row['title']) <= 256
                    or not row['title'].strip() or '\0' in row['title']
                    or type(row['fully_visible']) is not bool):
                return False
    if (result['status'] == 'viewport_unchanged'
            and viewports[-1]['rows'] != viewports[-2]['rows']):
        return False
    return True
