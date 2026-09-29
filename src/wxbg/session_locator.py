"""Read-only, bounded UIA ItemContainer probe for an offscreen session.

This never realizes, scrolls, focuses, clicks, or types. A database key is not
used as a UI ref. A found item is only a provider witness, not an opened chat.
"""
from .policy import AdapterError


ITEM_CONTAINER_PATTERN_ID = 10019
SCROLL_ITEM_PATTERN_ID = 10017
VIRTUALIZED_ITEM_PATTERN_ID = 10020
AUTOMATION_ID_PROPERTY_ID = 30011
LIST_ITEM_CONTROL_TYPE_ID = 50007
CONTAINER_CONTROL_TYPES = frozenset({'List', 'Group', 'Table', 'DataGrid'})


def _rect(node):
    try:
        value = node.rectangle()
        return [value.left, value.top, value.right, value.bottom]
    except Exception:
        return None


def _layout(adapter, nodes):
    rows = [node for node in nodes
            if node.element_info.class_name == 'mmui::ChatSessionCell']
    ancestors = []
    if rows:
        try:
            parent = rows[0].parent()
            for _ in range(10):
                if parent is None:
                    break
                info = parent.element_info
                ancestors.append({'class': info.class_name,
                                  'control_type': info.control_type,
                                  'rect': _rect(parent)})
                parent = parent.parent()
        except Exception:
            pass
    buttons = []
    for node in nodes:
        info = node.element_info
        if (info.class_name in ('mmui::XButton', 'mmui::XOutlineButton')
                and info.name in ('傳送檔案', '傳送', '发送', '發送', 'Send')):
            buttons.append({'class': info.class_name, 'name': info.name,
                            'rect': _rect(node)})
    result = {'visible_session_count': len(rows),
              'row_rects': [_rect(row) for row in rows[:25]],
              'first_row_ancestors': ancestors,
              'send_controls': buttons,
              'root_rect': None,
              'native': None}
    try:
        result['root_rect'] = _rect(adapter.root())
    except Exception:
        pass
    if hasattr(adapter, 'hwnd'):
        try:
            import win32gui
            renders = []
            win32gui.EnumChildWindows(adapter.hwnd,
                lambda hwnd, _: renders.append(hwnd)
                if win32gui.GetClassName(hwnd) == 'MMUIRenderSubWindowHW' else None,
                None)
            result['native'] = {
                'main_client': list(win32gui.GetClientRect(adapter.hwnd)),
                'main_client_screen_origin': list(win32gui.ClientToScreen(adapter.hwnd, (0, 0))),
                'render_count': len(renders),
                'render_client': (list(win32gui.GetClientRect(renders[0]))
                                  if len(renders) == 1 else None),
                'render_client_screen_origin': (list(win32gui.ClientToScreen(renders[0], (0, 0)))
                                                if len(renders) == 1 else None),
            }
        except Exception:
            result['native'] = {'observation_failed': True}
    return result


def _pattern_available(raw, pattern_id):
    try:
        return bool(raw.GetCurrentPattern(pattern_id))
    except Exception:
        return False


def probe_item_container(adapter, title):
    """Report whether one exact offscreen session is addressable by UIA."""
    if (type(title) is not str or not 1 <= len(title) <= 256
            or not title.strip() or '\0' in title):
        raise AdapterError('invalid_title')
    adapter.precondition()
    nodes = tuple(adapter.nodes())
    adapter.precondition()
    candidates = [node for node in nodes
                  if node.element_info.control_type in CONTAINER_CONTROL_TYPES]
    if len(candidates) > 256:
        raise AdapterError('tree_budget_exceeded')
    target_aid = 'session_item_' + title
    visible_exact_target_rects = [
        _rect(node) for node in nodes
        if (node.element_info.class_name == 'mmui::ChatSessionCell'
            and node.element_info.control_type == 'ListItem'
            and node.element_info.automation_id == target_aid)]
    containers = 0
    matches = []
    lookup_errors = []
    for node in candidates:
        adapter.precondition()
        raw = node.element_info.element
        try:
            pattern = raw.GetCurrentPattern(ITEM_CONTAINER_PATTERN_ID)
        except Exception:
            continue
        # comtypes returns a falsey NULL COM pointer, not Python None, when
        # the provider does not implement this optional UIA pattern.
        if not pattern:
            continue
        containers += 1
        try:
            container = pattern.QueryInterface(adapter.uia_item_container_interface)
        except Exception as exc:
            lookup_errors.append({'phase': 'interface', 'type': type(exc).__name__})
            continue
        try:
            item = container.FindItemByProperty(
                None, AUTOMATION_ID_PROPERTY_ID, target_aid)
        except Exception as exc:
            lookup_errors.append({'phase': 'find', 'type': type(exc).__name__})
            continue
        if item is None:
            continue
        try:
            if (item.CurrentAutomationId != target_aid
                    or item.CurrentClassName != 'mmui::ChatSessionCell'
                    or item.CurrentControlType != LIST_ITEM_CONTROL_TYPE_ID):
                raise AdapterError('item_container_match_unverified')
        except AdapterError:
            raise
        except Exception:
            raise AdapterError('item_container_match_unverified') from None
        matches.append(item)
    adapter.precondition()
    if len(matches) > 1:
        raise AdapterError('ambiguous_recipient')
    item = matches[0] if matches else None
    return {
        'target_found': item is not None,
        'conclusion': ('unknown' if lookup_errors else
                       'found' if item is not None else 'not_found'),
        'item_container_count': containers,
        'candidate_count': len(candidates),
        'lookup_error_count': len(lookup_errors),
        'lookup_errors': lookup_errors[:8],
        'scroll_item_available': bool(item and _pattern_available(item, SCROLL_ITEM_PATTERN_ID)),
        'virtualized_item_available': bool(item and _pattern_available(item, VIRTUALIZED_ITEM_PATTERN_ID)),
        'visible_exact_target_rects': visible_exact_target_rects,
        'navigation_performed': False,
        'background_mode': 'minimized',
        'layout': _layout(adapter, nodes),
    }
