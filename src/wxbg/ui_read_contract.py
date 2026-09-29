"""Closed results for the simple, currently exposed UI read tools."""

import re
from .sender_role import SENDER_ROLE_KEYS, validate_sender_role_fields


UI_READ_ACTIONS = frozenset({
    'status', 'list_sessions', 'read_messages', 'list_contacts',
})
_REF = re.compile(r'[0-9a-f]{32}\Z')
_STATUS_KEYS = frozenset({
    'status', 'pid', 'current_chat', 'strict_background',
    'auto_set_focus', 'account_identity_verified', 'layout_calibration',
})
_SESSION_KEYS = frozenset({'sessions', 'visible_only', 'count', 'current_chat'})
_MESSAGE_KEYS = frozenset({'chat', 'messages', 'visible_only', 'not_full_history'})
_CONTACT_KEYS = frozenset({
    'contacts', 'query', 'limit', 'count', 'exposed_count', 'visible_only',
    'not_full_directory', 'pagination_supported', 'contact_ref_scope',
    'query_scope', 'background_mode', 'scroll_steps',
    'bounded_scroll_supported', 'scroll_steps_limit',
    'original_contacts_view_restored', 'original_conversation_restored',
})
_OPEN_EVIDENCE_KEYS = frozenset({
    'session_ref', 'title', 'activation_started',
    'selected_and_header_verified',
})


def _name(value):
    return type(value) is str and bool(value.strip()) and '\0' not in value


def _ref(value):
    return type(value) is str and _REF.fullmatch(value) is not None


def _rect(value):
    return (type(value) is list and len(value) == 4
            and all(type(item) is int for item in value)
            and value[0] < value[2] and value[1] < value[3])


def _layout_calibration(value):
    if (type(value) is not dict or set(value) != {
            'root_rect', 'render_client', 'window_dpi',
            'coordinate_mapping_verified', 'send_button',
            'attachment_button', 'session_table'}):
        return False
    root, render = value['root_rect'], value['render_client']
    if (not _rect(root) or not _rect(render)
            or not 640 <= root[2]-root[0] <= 32767
            or not 480 <= root[3]-root[1] <= 32767
            or render != [0, 0, root[2]-root[0], root[3]-root[1]]
            or type(value['window_dpi']) is not int
            or not 72 <= value['window_dpi'] <= 768
            or value['coordinate_mapping_verified'] is not True):
        return False
    for name in ('send_button', 'attachment_button', 'session_table'):
        control = value[name]
        if control is None:
            continue
        if (type(control) is not dict or set(control) != {'rect', 'client_point'}
                or not _rect(control['rect'])
                or type(control['client_point']) is not list
                or len(control['client_point']) != 2
                or any(type(item) is not int for item in control['client_point'])):
            return False
        bounds = control['rect']
        if (not root[0]+2 <= bounds[0] < bounds[2] <= root[2]-2
                or not root[1]+2 <= bounds[1] < bounds[3] <= root[3]-2
                or control['client_point'] != [
                    (bounds[0]+bounds[2])//2-root[0],
                    (bounds[1]+bounds[3])//2-root[1]]):
            return False
        if name == 'send_button' and (control['client_point'][0] <= (root[2]-root[0])//2
                                      or control['client_point'][1] <= (root[3]-root[1])//2):
            return False
    return True


def _valid_desktop_snapshot(state):
    if type(state) is not dict:
        return False
    cursor, windows = state.get('cursor'), state.get('visible_windows')
    return (state.get('minimized') is True
            and type(state.get('capture')) is int and state['capture'] == 0
            and type(state.get('foreground')) is int
            and type(state.get('clipboard_sequence')) is int
            and type(cursor) is list and len(cursor) == 2
            and all(type(item) is int for item in cursor)
            and type(windows) is list
            and all(type(item) is int for item in windows)
            and state.get('cursor_api') == 'GetCursorPos'
            and state.get('cursor_dpi_context') == 'per_monitor_v2'
            and state.get('cursor_coordinate_space') == 'screen_coordinates_under_pm_v2')


def valid_ui_background(evidence):
    """Require the worker's complete no-focus monitor verdict, not just its body."""
    if (type(evidence) is not dict
            or evidence.get('background_observation_passed') is not True
            or type(evidence.get('observations')) is not int
            or evidence['observations'] < 1
            or any(evidence.get(key) is not False for key in (
                'foreground_changed', 'clipboard_changed',
                'target_restored', 'capture_observed'))
            or evidence.get('new_visible_windows') != []
            or evidence.get('monitor_errors') != []):
        return False
    before, after = evidence.get('before'), evidence.get('after')
    if not _valid_desktop_snapshot(before) or not _valid_desktop_snapshot(after):
        return False
    if any(before[key] != after[key] for key in (
            'foreground', 'clipboard_sequence', 'visible_windows')):
        return False
    return (type(evidence.get('cursor_changed')) is bool
            and (evidence['cursor_changed'] or before['cursor'] == after['cursor']))


def valid_open_session_evidence(evidence, session_ref):
    return (type(evidence) is dict and set(evidence) == _OPEN_EVIDENCE_KEYS
            and type(session_ref) is str and _ref(session_ref)
            and evidence['session_ref'] == session_ref
            and (evidence['title'] is None or _name(evidence['title']))
            and type(evidence['activation_started']) is bool
            and type(evidence['selected_and_header_verified']) is bool)


def valid_open_session_success(result, evidence, args):
    if (type(args) is not dict or set(args) != {'session_ref'}
            or not valid_open_session_evidence(evidence, args['session_ref'])
            or evidence['selected_and_header_verified'] is not True
            or type(result) is not dict
            or set(result) != {'title', 'status', 'verification_level', 'background_mode'}):
        return False
    return (result['title'] == evidence['title']
            and result['status'] == 'opened'
            and result['verification_level'] == 'client_ui'
            and result['background_mode'] == 'minimized')


def valid_ui_read_result(action, result, args, target=None):
    """Match only this worker's declared bounded result, without inventing rows."""
    if type(action) is not str or type(result) is not dict or type(args) is not dict:
        return False
    if action == 'status':
        return (set(result) == _STATUS_KEYS
                and result['status'] == 'ready'
                and type(result['pid']) is int and result['pid'] > 0
                and (target is None or type(target) is dict
                     and result['pid'] == target.get('pid'))
                and (result['current_chat'] is None
                     or _name(result['current_chat']))
                and result['strict_background'] is True
                and result['auto_set_focus'] is False
                and result['account_identity_verified'] is False
                and _layout_calibration(result['layout_calibration']))
    if action == 'list_sessions':
        query, limit = args.get('query', ''), args.get('limit', 100)
        if (type(query) is not str or type(limit) is not int or not 1 <= limit <= 100
                or set(result) != _SESSION_KEYS or result['visible_only'] is not True
                or (result['current_chat'] is not None
                    and not _name(result['current_chat']))):
            return False
        rows = result['sessions']
        if (type(rows) is not list or len(rows) > limit
                or type(result['count']) is not int or result['count'] != len(rows)):
            return False
        refs = set()
        for row in rows:
            if (type(row) is not dict or set(row) != {'ref', 'title'}
                    or not _ref(row['ref']) or not _name(row['title'])
                    or query.casefold() not in row['title'].casefold()
                    or row['ref'] in refs):
                return False
            refs.add(row['ref'])
        return True
    if action == 'read_messages':
        limit = args.get('limit', 50)
        if (type(limit) is not int or not 1 <= limit <= 200
                or set(result) != _MESSAGE_KEYS or not _name(result['chat'])
                or result['visible_only'] is not True
                or result['not_full_history'] is not True):
            return False
        rows = result['messages']
        if type(rows) is not list or len(rows) > limit:
            return False
        refs = set()
        for row in rows:
            if type(row) is not dict:
                return False
            row_keys=set(row)
            base_keys={'ref','type','text'}
            if row_keys not in (base_keys,base_keys|SENDER_ROLE_KEYS):
                return False
            if (not _ref(row['ref']) or type(row['type']) is not str
                    or not row['type'].startswith('mmui::Chat')
                    or not row['type'].endswith('ItemView')
                    or type(row['text']) is not str or row['ref'] in refs):
                return False
            if row_keys != base_keys:
                try:validate_sender_role_fields({key:row[key] for key in SENDER_ROLE_KEYS})
                except (TypeError,ValueError):return False
            refs.add(row['ref'])
        return True
    if action == 'list_contacts':
        query, limit, steps = (args.get('query', ''), args.get('limit', 100),
                               args.get('scroll_steps', 0))
        if (type(query) is not str or type(limit) is not int or not 1 <= limit <= 100
                or type(steps) is not int or not 0 <= steps <= 24):
            return False
        keys = _CONTACT_KEYS | ({'scroll_origin', 'viewport_changed'} if steps else set())
        if (set(result) != keys or result['query'] != query
                or type(result['limit']) is not int or result['limit'] != limit
                or type(result['scroll_steps']) is not int or result['scroll_steps'] != steps
                or result['visible_only'] is not True
                or result['not_full_directory'] is not True
                or result['pagination_supported'] is not False
                or result['contact_ref_scope'] != 'temporary_ui_contacts'
                or result['query_scope'] != ('requested_contact_view_only' if steps
                                              else 'exposed_contacts_only')
                or result['background_mode'] != 'minimized'
                or result['bounded_scroll_supported'] is not True
                or type(result['scroll_steps_limit']) is not int
                or result['scroll_steps_limit'] != 24
                or result['original_contacts_view_restored'] is not True
                or result['original_conversation_restored'] is not True):
            return False
        if steps and (result['scroll_origin'] != 'observed_top'
                      or type(result['viewport_changed']) is not bool):
            return False
        rows = result['contacts']
        if (type(rows) is not list or len(rows) > limit
                or type(result['count']) is not int or result['count'] != len(rows)
                or type(result['exposed_count']) is not int
                or not len(rows) <= result['exposed_count'] <= 2500):
            return False
        refs = set()
        for row in rows:
            if (type(row) is not dict or set(row) != {'contact_ref', 'display_text'}
                    or not _ref(row['contact_ref'])
                    or type(row['display_text']) is not str
                    or query.casefold() not in row['display_text'].casefold()
                    or row['contact_ref'] in refs):
                return False
            refs.add(row['contact_ref'])
        return True
    return False
