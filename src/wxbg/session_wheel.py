"""One live-layout, target-local wheel notch in the Chats session list.

The caller owns the bounded navigation, context checks and outcome journal.
There is no generic pointer-input API and no automatic retry on uncertainty.
"""
import ctypes
from ctypes import wintypes as W

import psutil
import win32api
import win32gui
import win32process

from .monitor import GUITHREADINFO
from .observed_adapter import rectangle
from .policy import AdapterError
from .window_geometry import main_client_size_matches_or_minimized


USER32 = ctypes.WinDLL('user32', use_last_error=True)
USER32.SendMessageTimeoutW.argtypes = (W.HWND, W.UINT, W.WPARAM, W.LPARAM,
    W.UINT, W.UINT, ctypes.POINTER(ctypes.c_size_t))
USER32.SendMessageTimeoutW.restype = W.LPARAM
USER32.GetGUIThreadInfo.argtypes = (W.DWORD, ctypes.POINTER(GUITHREADINFO))
USER32.GetGUIThreadInfo.restype = W.BOOL
USER32.GetThreadDpiAwarenessContext.restype = W.HANDLE
USER32.GetAwarenessFromDpiAwarenessContext.argtypes = (W.HANDLE,)
USER32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int

def _quiet(thread):
    if any(win32api.GetAsyncKeyState(key) & 0x8000
           for key in (0x10, 0x11, 0x12, 0x5b, 0x5c, 1, 2, 4, 5, 6)):
        raise AdapterError('modifier_or_mouse_button_pressed')
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not USER32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        raise AdapterError('capture_observation_failed')
    if info.hwndCapture:
        raise AdapterError('native_capture_active')


def _frame(adapter, geometry, expected=None):
    thread, pid = win32process.GetWindowThreadProcessId(adapter.hwnd)
    if (pid != adapter.pid or psutil.Process(pid).create_time() != adapter.created
            or win32gui.GetClassName(adapter.hwnd) != 'Qt51514QWindowIcon'
            or expected is not None and (thread, pid) != expected[:2]):
        raise AdapterError('stale_process')
    _quiet(thread)
    if USER32.GetAwarenessFromDpiAwarenessContext(
            USER32.GetThreadDpiAwarenessContext()) != 2:
        raise AdapterError('coordinate_context_unverified')
    renders = []
    win32gui.EnumChildWindows(adapter.hwnd,
        lambda hwnd, _: renders.append(hwnd)
        if win32gui.GetClassName(hwnd) == 'MMUIRenderSubWindowHW' else None,
        None)
    if (len(renders) != 1
            or win32process.GetWindowThreadProcessId(renders[0])[1] != pid
            or not main_client_size_matches_or_minimized(
                adapter.hwnd,
                (0, 0, geometry['root_rect'][2] - geometry['root_rect'][0],
                 geometry['root_rect'][3] - geometry['root_rect'][1]), win32gui)
            or tuple(win32gui.GetClientRect(renders[0])) !=
               (0, 0, geometry['root_rect'][2] - geometry['root_rect'][0],
                geometry['root_rect'][3] - geometry['root_rect'][1])
            or tuple(win32gui.ClientToScreen(adapter.hwnd, (0, 0))) !=
               geometry['root_rect'][:2]
            or win32gui.ClientToScreen(adapter.hwnd, (0, 0))
               != win32gui.ClientToScreen(renders[0], (0, 0))
            or expected is not None and renders[0] != expected[2]):
        raise AdapterError('unverified_geometry')
    screen = geometry['screen_point']
    client = tuple(win32gui.ScreenToClient(adapter.hwnd, screen))
    if (tuple(win32gui.ClientToScreen(adapter.hwnd, client)) != screen
            or not (0 < client[0] < geometry['root_rect'][2] - geometry['root_rect'][0]
                    and 0 < client[1] < geometry['root_rect'][3] - geometry['root_rect'][1])
            or not all(type(value) is int and -32768 <= value <= 32767
                       for value in screen)
            or expected is not None and screen != expected[3]):
        raise AdapterError('coordinate_mapping_unverified')
    return thread, pid, renders[0], screen


def _raw_rect(raw):
    rect = raw.CurrentBoundingRectangle
    return rect.left, rect.top, rect.right, rect.bottom


def _current_table(adapter, table, expected=None):
    info = table.element_info
    if (info.class_name, info.control_type) != ('mmui::XTableView', 'List'):
        raise AdapterError('session_view_changed')
    retained_id = tuple(info.runtime_id)
    if not retained_id:
        raise AdapterError('session_view_changed')
    root = adapter.root()
    root_rect = rectangle(root)
    root_id = tuple(root.element_info.runtime_id)
    table_rect = rectangle(table)
    parent_node = table.parent()
    if (root.element_info.class_name != 'mmui::MainWindow'
            or not root_id
            or parent_node is None
            or (parent_node.element_info.class_name,
                parent_node.element_info.control_type) != ('mmui::ChatSessionList', 'Group')
            or rectangle(parent_node) != table_rect
            or not all(type(value) is int for value in (*root_rect, *table_rect))
            or table_rect[2] - table_rect[0] < 160
            or table_rect[3] - table_rect[1] < 160
            or not (root_rect[0] < table_rect[0] < table_rect[2] < root_rect[2]
                    and root_rect[1] < table_rect[1] < table_rect[3] < root_rect[3])):
        raise AdapterError('unverified_layout')
    # SDK UIA_ClassNamePropertyId=30012, TreeScope_Descendants=4.
    matches = root.element_info.element.FindAll(4,
        adapter.client.CreatePropertyCondition(30012, 'mmui::XTableView'))
    if matches.Length != 1:
        raise AdapterError('session_view_changed')
    current = matches.GetElement(0)
    if (current.CurrentClassName != 'mmui::XTableView'
            or current.CurrentControlType != 50008
            or _raw_rect(current) != table_rect
            or tuple(current.GetRuntimeId()) != retained_id):
        raise AdapterError('session_view_changed')
    parent = adapter.client.ControlViewWalker.GetParentElement(current)
    if (not parent or parent.CurrentClassName != 'mmui::ChatSessionList'
            or parent.CurrentControlType != 50026
            or _raw_rect(parent) != table_rect):
        raise AdapterError('session_view_changed')
    screen_point = ((table_rect[0] + table_rect[2]) // 2,
                    (table_rect[1] + table_rect[3]) // 2)
    if not (table_rect[0] + 8 < screen_point[0] < table_rect[2] - 8
            and table_rect[1] + 8 < screen_point[1] < table_rect[3] - 8):
        raise AdapterError('coordinate_mapping_unverified')
    geometry = {'root_rect': root_rect, 'table_rect': table_rect,
                'root_id': root_id, 'table_id': retained_id,
                'screen_point': screen_point}
    if expected is not None and geometry != expected:
        raise AdapterError('session_view_changed')
    return geometry


def send_session_wheel(adapter, table, delta, *, check_context):
    """Deliver one signed wheel notch; errors retain whether delivery began."""
    if type(delta) is not int or delta not in (-120, 120):
        raise AdapterError('invalid_session_wheel')
    if not callable(check_context):
        raise AdapterError('missing_context_guard')
    started = False
    try:
        adapter.precondition()
        check_context()
        geometry = _current_table(adapter, table)
        original = _frame(adapter, geometry)
        check_context()
        adapter.precondition()
        current = _current_table(adapter, table, expected=geometry)
        if current != geometry:
            raise AdapterError('session_view_changed')
        frame = _frame(adapter, current, original)
        screen = current['screen_point']
        value = ctypes.c_size_t()
        started = True
        try:
            ok = USER32.SendMessageTimeoutW(adapter.hwnd, 0x20a,
                (delta & 0xffff) << 16,
                ((screen[1] & 0xffff) << 16) | (screen[0] & 0xffff),
                0x2 | 0x20, 1000, ctypes.byref(value))
        except Exception:
            raise AdapterError('wheel_result_unknown') from None
        if not ok:
            raise AdapterError('wheel_result_unknown')
        _frame(adapter, geometry, original)
        _current_table(adapter, table, expected=geometry)
        adapter.precondition()
        check_context()
    except Exception as exc:
        error = exc if isinstance(exc, AdapterError) else AdapterError('session_wheel_observation_failed')
        error.wheel_delivery_started = started
        raise error from None
