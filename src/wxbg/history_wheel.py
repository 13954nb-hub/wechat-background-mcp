"""Private, observed-layout history wheel primitive. No generic input API.

This verifies sequential boundaries, not atomic UI state. A successful return
means finite message delivery only, not scrolling or restoration. The caller
owns the guardian, context and destination observations.
"""
import ctypes
from ctypes import wintypes as W

import psutil
import win32api
import win32gui
import win32process

from wxbg.monitor import GUITHREADINFO
from wxbg.observed_adapter import rectangle
from wxbg.policy import AdapterError


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


def _native_frame(adapter, expected=None):
    """Recompute the native mapping from the current semantic viewport."""
    thread, pid = win32process.GetWindowThreadProcessId(adapter.hwnd)
    if (pid != adapter.pid or psutil.Process(pid).create_time() != adapter.created
            or expected is not None and (thread, pid) != expected[:2]):
        raise AdapterError('stale_process')
    _quiet(thread)
    if USER32.GetAwarenessFromDpiAwarenessContext(USER32.GetThreadDpiAwarenessContext()) != 2:
        raise AdapterError('coordinate_context_unverified')
    root = adapter.root()
    root_rect = rectangle(root)
    if (root.element_info.class_name != 'mmui::MainWindow'
            or root.element_info.control_type != 'Window'
            or not 0 < root_rect[2] - root_rect[0] <= 32767
            or not 0 < root_rect[3] - root_rect[1] <= 32767):
        raise AdapterError('unverified_layout')
    view = _find_one(adapter, root.element_info.element, 'mmui::MessageView')
    view_rect = _raw_rect(view)
    if view.CurrentControlType != 50026:  # UIA_GroupControlTypeId
        raise AdapterError('history_view_changed')
    view_id = _identity(view)
    if not (root_rect[0] <= view_rect[0] < view_rect[2] <= root_rect[2]
            and root_rect[1] <= view_rect[1] < view_rect[3] <= root_rect[3]):
        raise AdapterError('unverified_layout')
    point = ((view_rect[0] + view_rect[2]) // 2 - root_rect[0],
             (view_rect[1] + view_rect[3]) // 2 - root_rect[1])
    renders = []
    win32gui.EnumChildWindows(adapter.hwnd, lambda hwnd, _: renders.append(hwnd)
        if win32gui.GetClassName(hwnd) == 'MMUIRenderSubWindowHW' else None, None)
    render_rect = tuple(win32gui.GetClientRect(renders[0])) if len(renders) == 1 else None
    if (len(renders) != 1 or win32process.GetWindowThreadProcessId(renders[0])[1] != pid
            or render_rect != (0, 0, root_rect[2] - root_rect[0], root_rect[3] - root_rect[1])
            or win32gui.ClientToScreen(adapter.hwnd, (0, 0))
               != win32gui.ClientToScreen(renders[0], (0, 0))
            or expected is not None and renders[0] != expected[2]):
        raise AdapterError('unverified_geometry')
    if expected is not None and (root_rect, view_rect, point, view_id) != expected[4:8]:
        raise AdapterError('unverified_layout')
    screen = tuple(win32gui.ClientToScreen(adapter.hwnd, point))
    if (tuple(win32gui.ScreenToClient(adapter.hwnd, screen)) != point
            or not all(type(value) is int and -32768 <= value <= 32767 for value in screen)
            or expected is not None and screen != expected[3]):
        raise AdapterError('coordinate_mapping_unverified')
    return thread, pid, renders[0], screen, root_rect, view_rect, point, view_id


def _raw_rect(raw):
    value = raw.CurrentBoundingRectangle
    return value.left, value.top, value.right, value.bottom


def _identity(raw):
    result = tuple(raw.GetRuntimeId())
    if not result:
        raise AdapterError('history_view_changed')
    return result


def _verify_raw(raw, kind, control, view_rect):
    if (not raw or raw.CurrentClassName != kind or raw.CurrentControlType != control
            or _raw_rect(raw) != view_rect):
        raise AdapterError('history_view_changed')


def _find_one(adapter, parent, kind):
    # UIA_ClassNamePropertyId=30012; TreeScope_Descendants=4.
    matches = parent.FindAll(4, adapter.client.CreatePropertyCondition(30012, kind))
    if matches.Length != 1:
        raise AdapterError('history_view_changed')
    return matches.GetElement(0)


def _current_viewport(adapter, list_node, root_rect):
    root = adapter.root()
    if root.element_info.class_name != 'mmui::MainWindow' or rectangle(root) != root_rect:
        raise AdapterError('unverified_layout')
    view = _find_one(adapter, root.element_info.element, 'mmui::MessageView')
    view_rect = _raw_rect(view)
    if not (root_rect[0] <= view_rect[0] < view_rect[2] <= root_rect[2]
            and root_rect[1] <= view_rect[1] < view_rect[3] <= root_rect[3]):
        raise AdapterError('history_view_changed')
    info = list_node.element_info
    if ((info.class_name, info.control_type) != ('mmui::RecyclerListView', 'List')
            or rectangle(list_node) != view_rect):
        raise AdapterError('history_view_changed')
    retained_id = tuple(info.runtime_id)
    if not retained_id:
        raise AdapterError('history_view_changed')
    _verify_raw(view, 'mmui::MessageView', 50026, view_rect)  # UIA_GroupControlTypeId
    view_id = _identity(view)
    current = _find_one(adapter, view, 'mmui::RecyclerListView')
    _verify_raw(current, 'mmui::RecyclerListView', 50008, view_rect)  # UIA_ListControlTypeId
    if _identity(current) != retained_id:
        raise AdapterError('history_view_changed')
    # Same ControlView parent relationship as pywinauto UIAElementInfo.parent.
    parent = adapter.client.ControlViewWalker.GetParentElement(current)
    _verify_raw(parent, 'mmui::MessageView', 50026, view_rect)
    if _identity(parent) != view_id:
        raise AdapterError('history_view_changed')
    return view_rect


def send_history_wheel(adapter, list_node, delta, *, check_context):
    """One target-local notch; exceptions identify possible input side effects.

    check_context is a mandatory zero-argument guard supplied by the caller. It
    must raise on changed session, draft or other experiment preconditions.
    No automatic retry, navigation, focus operation or restoration occurs here.
    """
    if type(delta) is not int or delta not in (-120, 120):
        raise AdapterError('invalid_history_wheel')
    if not callable(check_context):
        raise AdapterError('missing_context_guard')
    started = False
    try:
        adapter.precondition()
        check_context()
        original_frame = _native_frame(adapter)
        if _current_viewport(adapter, list_node, original_frame[4]) != original_frame[5]:
            raise AdapterError('history_view_changed')
        check_context()
        adapter.precondition()
        if _current_viewport(adapter, list_node, original_frame[4]) != original_frame[5]:
            raise AdapterError('history_view_changed')
        check_context()
        adapter.precondition()
        frame = _native_frame(adapter, original_frame)
        screen = frame[3]
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
        _native_frame(adapter, original_frame)
        adapter.precondition()
        check_context()
    except Exception as exc:
        error = exc if isinstance(exc, AdapterError) else AdapterError('history_wheel_observation_failed')
        error.wheel_delivery_started = started
        raise error from None
