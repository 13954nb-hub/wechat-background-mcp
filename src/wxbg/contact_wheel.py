"""Private target-local Contacts wheel primitive; no generic MCP input API."""
import ctypes
from ctypes import wintypes as W
import psutil
import win32api
import win32gui
import win32process

from .observed_adapter import rectangle
from .policy import AdapterError
from .render_surface import enumerate_render_surfaces
from .monitor import GUITHREADINFO

USER32 = ctypes.WinDLL('user32', use_last_error=True)
USER32.SendMessageTimeoutW.argtypes = (W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT, ctypes.POINTER(ctypes.c_size_t))
USER32.SendMessageTimeoutW.restype = W.LPARAM
USER32.GetGUIThreadInfo.argtypes = (W.DWORD, ctypes.POINTER(GUITHREADINFO))
USER32.GetGUIThreadInfo.restype = W.BOOL
USER32.GetThreadDpiAwarenessContext.restype = W.HANDLE
USER32.GetAwarenessFromDpiAwarenessContext.argtypes = (W.HANDLE,)
USER32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int

def _quiet(thread):
    if any(win32api.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5b, 0x5c, 1, 2, 4, 5, 6)):
        raise AdapterError('modifier_or_mouse_button_pressed')
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    if not USER32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        raise AdapterError('capture_observation_failed')
    if info.hwndCapture:
        raise AdapterError('native_capture_active')


def _current_table(adapter, table, root):
    # SDK UIA_ClassNamePropertyId=30012, UIA_AutomationIdPropertyId=30011,
    # TreeScope_Descendants=4. One provider-side filtered lookup avoids a full
    # Python descendant walk for each notch. A retained stale table alone is
    # insufficient: prove it remains reachable from the current live root and
    # that a chat input is not also exposed.
    condition = adapter.client.CreateOrCondition(
        adapter.client.CreatePropertyCondition(30012, 'mmui::ContactsTableBaseView'),
        adapter.client.CreatePropertyCondition(30011, 'chat_input_field'))
    matches = root.element_info.element.FindAll(4, condition)
    if matches.Length != 1:
        raise AdapterError('contacts_page_changed')
    current = matches.GetElement(0)
    raw_bounds = current.CurrentBoundingRectangle
    current_rect = (raw_bounds.left, raw_bounds.top, raw_bounds.right, raw_bounds.bottom)
    if (current.CurrentClassName != 'mmui::ContactsTableBaseView'
            or tuple(current.GetRuntimeId()) != tuple(table.element_info.runtime_id)
            or current_rect != rectangle(table)):
        raise AdapterError('contacts_page_changed')


def send_contact_wheel(adapter, table, delta):
    if type(delta) is not int or delta not in (-120, 120):
        raise AdapterError('invalid_contact_wheel')
    adapter.precondition()
    thread, pid = win32process.GetWindowThreadProcessId(adapter.hwnd)
    if pid != adapter.pid or psutil.Process(pid).create_time() != adapter.created:
        raise AdapterError('stale_process')
    _quiet(thread)
    if USER32.GetAwarenessFromDpiAwarenessContext(USER32.GetThreadDpiAwarenessContext()) != 2:
        raise AdapterError('coordinate_context_unverified')
    root = adapter.root()
    root_rect = rectangle(root)
    if (not 0 < root_rect[2] - root_rect[0] <= 32767
            or not 0 < root_rect[3] - root_rect[1] <= 32767):
        raise AdapterError('unverified_layout')
    table_rect = rectangle(table)
    render_rect = (0, 0, root_rect[2] - root_rect[0], root_rect[3] - root_rect[1])
    point = ((table_rect[0] + table_rect[2]) // 2 - root_rect[0],
             (table_rect[1] + table_rect[3]) // 2 - root_rect[1])
    info = table.element_info
    if ((info.class_name, info.control_type) != ('mmui::ContactsTableBaseView', 'Group')
            or not (root_rect[0] < table_rect[0] < table_rect[2] <= root_rect[2]
                    and root_rect[1] <= table_rect[1] < table_rect[3] <= root_rect[3])):
        raise AdapterError('contacts_page_changed')
    renders = enumerate_render_surfaces(adapter.hwnd, win32gui)
    if (len(renders) != 1 or win32process.GetWindowThreadProcessId(renders[0])[1] != pid
            or tuple(win32gui.GetClientRect(renders[0])) != render_rect
            or win32gui.ClientToScreen(adapter.hwnd, (0, 0))
               != win32gui.ClientToScreen(renders[0], (0, 0))):
        raise AdapterError('unverified_geometry')
    if not (table_rect[0] < point[0] + root_rect[0] < table_rect[2]
            and table_rect[1] < point[1] + root_rect[1] < table_rect[3]):
        raise AdapterError('unverified_geometry')
    screen = win32gui.ClientToScreen(adapter.hwnd, point)
    if (win32gui.ScreenToClient(adapter.hwnd, screen) != point
            or not all(-32768 <= p <= 32767 for p in screen)):
        raise AdapterError('coordinate_mapping_unverified')
    adapter.precondition()
    _current_table(adapter, table, root)
    adapter.precondition()
    # UIA calls above can block. Revalidate ownership after them, immediately
    # before finite delivery; this is still sequential, never atomic with UI.
    if (tuple(win32process.GetWindowThreadProcessId(adapter.hwnd)) != (thread, pid)
            or psutil.Process(pid).create_time() != adapter.created):
        raise AdapterError('stale_process')
    _quiet(thread)
    if USER32.GetAwarenessFromDpiAwarenessContext(USER32.GetThreadDpiAwarenessContext()) != 2:
        raise AdapterError('coordinate_context_unverified')
    if rectangle(adapter.root()) != root_rect:
        raise AdapterError('unverified_layout')
    if rectangle(table) != table_rect:
        raise AdapterError('contacts_page_changed')
    if (tuple(win32gui.GetClientRect(renders[0])) != render_rect
            or win32process.GetWindowThreadProcessId(renders[0])[1] != pid
            or win32gui.ClientToScreen(adapter.hwnd, (0, 0))
               != win32gui.ClientToScreen(renders[0], (0, 0))):
        raise AdapterError('unverified_geometry')
    if (tuple(win32gui.ClientToScreen(adapter.hwnd, point)) != tuple(screen)
            or tuple(win32gui.ScreenToClient(adapter.hwnd, screen)) != point):
        raise AdapterError('coordinate_mapping_unverified')
    value = ctypes.c_size_t()
    ok = USER32.SendMessageTimeoutW(adapter.hwnd, 0x20a, (delta & 0xffff) << 16,
        ((screen[1] & 0xffff) << 16) | (screen[0] & 0xffff), 0x2 | 0x20, 1000, ctypes.byref(value))
    if not ok:
        raise AdapterError('wheel_result_unknown')
    _quiet(thread)
    adapter.precondition()
