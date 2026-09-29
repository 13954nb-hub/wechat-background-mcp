"""Private wheel contract: every Win32/UIA boundary is fake; no window input."""
from contextlib import ExitStack
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from wxbg import history_wheel as hw
from wxbg.policy import AdapterError


ROOT_RECT = (0, 0, 3240, 2040)
VIEW_RECT = (667, 200, 3229, 1680)
POINT = (1948, 940)
CURRENT_ROOT_RECT = (2, 0, 3237, 1995)
CURRENT_VIEW_RECT = (670, 200, 3227, 1635)
CURRENT_RENDER_RECT = (0, 0, 3235, 1995)
CURRENT_POINT = (1946, 917)


def rect(values):
    return NS(**dict(zip(('left', 'top', 'right', 'bottom'), values)))


class Raw:
    def __init__(self, kind, control, runtime, bounds):
        self.CurrentClassName = kind
        self.CurrentControlType = control
        self.CurrentBoundingRectangle = rect(bounds)
        self.runtime = runtime
        self.children = []
        self.parent = None
        self.on_find = None
    def GetRuntimeId(self): return self.runtime
    def FindAll(self, scope, condition):
        if self.on_find: self.on_find()
        assert scope == 4
        found = [n for n in self.children if n.CurrentClassName == condition[1]]
        return NS(Length=len(found), GetElement=lambda index: found[index])


class Node:
    def __init__(self, raw, control):
        self.raw = raw
        self.element_info = NS(class_name=raw.CurrentClassName, control_type=control,
                               runtime_id=raw.runtime, element=raw)
    def rectangle(self): return self.raw.CurrentBoundingRectangle


class Adapter:
    hwnd = 100
    pid = 200
    created = 300.0
    def __init__(self):
        self.block = None
        self.events = []
        self.root_raw = Raw('mmui::MainWindow', 50032, (42, 100, 1), ROOT_RECT)
        self.view = Raw('mmui::MessageView', 50026, (42, 100, 2), VIEW_RECT)
        self.list = Raw('mmui::RecyclerListView', 50008, (42, 100, 3), VIEW_RECT)
        self.root_raw.children = [self.view]
        self.view.children = [self.list]
        self.list.parent = self.view
        self.root_node = Node(self.root_raw, 'Window')
        self.list_node = Node(self.list, 'List')
        self.client = NS(CreatePropertyCondition=lambda *args: args,
                         ControlViewWalker=NS(GetParentElement=lambda raw: raw.parent))
    def root(self):
        self.events.append('root')
        return self.root_node
    def precondition(self):
        self.events.append('precondition')
        if self.block: raise AdapterError(self.block)


class Native:
    def __init__(self):
        self.sent = []
        self.capture = 0
        self.capture_ok = True
        self.awareness = 2
        self.ok = True
        self.on_send = None
    def GetGUIThreadInfo(self, thread, info):
        info._obj.hwndCapture = self.capture
        return self.capture_ok
    def GetThreadDpiAwarenessContext(self): return -3
    def GetAwarenessFromDpiAwarenessContext(self, context): return self.awareness
    def SendMessageTimeoutW(self, *args):
        self.sent.append(args[:-1])
        if self.on_send: self.on_send()
        return self.ok


class HistoryWheelTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        self.native = Native()
        self.context = Mock(side_effect=lambda: self.adapter.events.append('context'))
        self.renders = [101]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mocks = {}
        specs = [(hw.psutil, 'Process', NS(create_time=lambda: 300.0)),
                 (hw.win32process, 'GetWindowThreadProcessId', (7, 200)),
                 (hw.win32api, 'GetAsyncKeyState', 0),
                 (hw.win32gui, 'GetClassName', 'MMUIRenderSubWindowHW'),
                 (hw.win32gui, 'GetClientRect', ROOT_RECT),
                 (hw.win32gui, 'ClientToScreen', (-30036, -31060)),
                 (hw.win32gui, 'ScreenToClient', POINT)]
        for owner, name, result in specs:
            self.mocks[name] = self.stack.enter_context(patch.object(owner, name, return_value=result))
        self.stack.enter_context(patch.object(hw, 'USER32', self.native))
        self.stack.enter_context(patch.object(hw.win32gui, 'EnumChildWindows',
            side_effect=lambda main, fn, _: [fn(h, None) for h in self.renders]))

    def call(self, delta=-120):
        return hw.send_history_wheel(self.adapter, self.adapter.list_node, delta,
                                     check_context=self.context)

    def use_current_measured_layout(self):
        self.adapter.root_raw.CurrentBoundingRectangle = rect(CURRENT_ROOT_RECT)
        self.adapter.view.CurrentBoundingRectangle = rect(CURRENT_VIEW_RECT)
        self.adapter.list.CurrentBoundingRectangle = rect(CURRENT_VIEW_RECT)
        self.mocks['GetClientRect'].return_value = CURRENT_RENDER_RECT
        self.mocks['ClientToScreen'].return_value = (-30038, -31083)
        self.mocks['ScreenToClient'].return_value = CURRENT_POINT

    def test_verified_current_render_client_passes_native_preflight(self):
        self.use_current_measured_layout()
        frame = hw._native_frame(self.adapter)
        self.assertEqual(frame[:3], (7, 200, 101))
        self.assertEqual(self.native.sent, [])

    def test_current_measured_layout_delivers_one_bounded_notch(self):
        self.use_current_measured_layout()
        self.call()
        self.assertEqual(len(self.native.sent), 1)
        self.mocks['ClientToScreen'].assert_called_with(100, CURRENT_POINT)

    def test_new_window_size_uses_live_view_center_for_wheel(self):
        self.adapter.root_raw.CurrentBoundingRectangle = rect((300, 80, 1900, 1080))
        self.adapter.view.CurrentBoundingRectangle = rect((680, 200, 1800, 800))
        self.adapter.list.CurrentBoundingRectangle = rect((680, 200, 1800, 800))
        self.mocks['GetClientRect'].return_value = (0, 0, 1600, 1000)
        self.mocks['ScreenToClient'].return_value = (940, 420)
        self.call()
        self.assertEqual(len(self.native.sent), 1)
        self.mocks['ClientToScreen'].assert_any_call(100, (940, 420))

    def test_current_render_with_old_root_and_view_never_delivers(self):
        self.mocks['GetClientRect'].return_value = CURRENT_RENDER_RECT
        self.mocks['ScreenToClient'].return_value = CURRENT_POINT
        self.reject('unverified_geometry')

    def test_current_root_and_view_with_old_render_never_delivers(self):
        self.use_current_measured_layout()
        self.mocks['GetClientRect'].return_value = ROOT_RECT
        self.mocks['ScreenToClient'].return_value = POINT
        self.reject('unverified_geometry')

    def reject(self, code, *, started=False):
        with self.assertRaises(AdapterError) as got: self.call()
        self.assertEqual(got.exception.code, code)
        self.assertEqual(bool(getattr(got.exception, 'wheel_delivery_started', False)), started)
        self.assertEqual(len(self.native.sent), int(started))
        return got.exception

    def test_one_main_window_delivery_signed_coordinates_and_finite_flags(self):
        self.assertIsNone(self.call())
        self.assertEqual(self.native.sent, [(100, 0x20a, 0xff88 << 16,
            ((-31060 & 0xffff) << 16) | (-30036 & 0xffff), 0x22, 1000)])
        self.mocks['ClientToScreen'].assert_called_with(100, POINT)

    def test_positive_inverse_is_one_notch(self):
        self.call(120)
        self.assertEqual(self.native.sent[0][2], 120 << 16)

    def test_delta_is_exact_int_and_only_two_values(self):
        for delta in (True, False, 120.0, '120', 0, 1, -1, 240, -240, None):
            with self.subTest(delta=delta), self.assertRaises(AdapterError) as got: self.call(delta)
            self.assertEqual(got.exception.code, 'invalid_history_wheel')
            self.assertFalse(getattr(got.exception, 'wheel_delivery_started', False))
        self.assertFalse(self.native.sent)

    def test_context_callback_is_required(self):
        with self.assertRaises(TypeError):
            hw.send_history_wheel(self.adapter, self.adapter.list_node, -120)
        with self.assertRaises(AdapterError) as got:
            hw.send_history_wheel(self.adapter, self.adapter.list_node, -120, check_context=None)
        self.assertEqual(got.exception.code, 'missing_context_guard')
        self.assertFalse(self.native.sent)

    def test_context_guards_bracket_com_and_follow_delivery(self):
        self.adapter.root_raw.on_find = lambda: self.adapter.events.append('find_view')
        self.adapter.view.on_find = lambda: self.adapter.events.append('find_list')
        self.native.on_send = lambda: self.adapter.events.append('send')
        self.call()
        events = self.adapter.events
        first = events.index('find_view'); last = events.index('find_list'); sent = events.index('send')
        self.assertIn('context', events[:first])
        self.assertIn('context', events[last + 1:sent])
        self.assertIn('context', events[sent + 1:])

    def test_context_failure_before_query_blocks_delivery(self):
        self.context.side_effect = AdapterError('context_conflict')
        self.reject('context_conflict')

    def test_context_change_during_com_blocks_delivery(self):
        self.adapter.view.on_find = lambda: setattr(self.context, 'side_effect', AdapterError('draft_conflict'))
        self.reject('draft_conflict')

    def test_context_change_after_delivery_is_started(self):
        self.native.on_send = lambda: setattr(self.context, 'side_effect', AdapterError('context_conflict'))
        self.reject('context_conflict', started=True)

    def test_existing_popup_precondition_blocks_delivery(self):
        self.adapter.block = 'existing_popup'
        self.reject('existing_popup')

    def test_retained_wrapper_kind_and_geometry_are_checked(self):
        self.adapter.list_node.element_info.class_name = 'Other'
        self.reject('history_view_changed')

    def test_list_control_type_is_required(self):
        self.adapter.list.CurrentControlType = 50026
        self.reject('history_view_changed')

    def test_message_view_control_type_is_required(self):
        self.adapter.view.CurrentControlType = 50008
        self.reject('history_view_changed')

    def test_fresh_root_has_exactly_one_message_view(self):
        self.adapter.root_raw.children = []
        self.reject('history_view_changed')
        self.adapter.root_raw.children = [self.adapter.view, self.adapter.view]
        self.reject('history_view_changed')

    def test_message_view_has_exactly_one_descendant_list(self):
        self.adapter.view.children = []
        self.reject('history_view_changed')
        self.adapter.view.children = [self.adapter.list, self.adapter.list]
        self.reject('history_view_changed')

    def test_fresh_runtime_matches_retained_list(self):
        self.adapter.list.runtime = (42, 100, 99)
        self.reject('history_view_changed')

    def test_missing_runtime_is_not_an_identity(self):
        self.adapter.list.runtime = ()
        self.adapter.list_node.element_info.runtime_id = ()
        self.reject('history_view_changed')

    def test_list_must_be_direct_child_of_the_same_message_view(self):
        self.adapter.list.parent = Raw('mmui::MessageView', 50026, (42, 100, 99), VIEW_RECT)
        self.reject('history_view_changed')
        self.adapter.list.parent = Raw('mmui::IntermediatePane', 50026, (42, 100, 2), VIEW_RECT)
        self.reject('history_view_changed')

    def test_message_view_geometry_is_exact(self):
        self.adapter.view.CurrentBoundingRectangle = rect((667, 200, 3229, 1679))
        self.reject('coordinate_mapping_unverified')

    def test_list_geometry_is_exact(self):
        self.adapter.list.CurrentBoundingRectangle = rect((667, 199, 3229, 1680))
        self.reject('history_view_changed')

    def test_root_frame_geometry_is_exact(self):
        self.adapter.root_raw.CurrentBoundingRectangle = rect((0, 0, 1296, 816))
        self.reject('unverified_layout')

    def test_provider_failure_is_sanitized_and_not_started(self):
        self.adapter.root_raw.on_find = Mock(side_effect=RuntimeError('private provider data'))
        error = self.reject('history_wheel_observation_failed')
        self.assertNotIn('private provider data', str(error))

    def test_window_pid_and_process_creation_rechecked_after_com(self):
        self.adapter.view.on_find = lambda: setattr(self.mocks['GetWindowThreadProcessId'], 'return_value', (7, 201))
        self.reject('stale_process')

    def test_changed_process_creation_blocks_delivery(self):
        self.mocks['Process'].return_value = NS(create_time=lambda: 301.0)
        self.reject('stale_process')

    def test_changed_thread_blocks_delivery(self):
        self.adapter.view.on_find = lambda: setattr(self.mocks['GetWindowThreadProcessId'], 'return_value', (8, 200))
        self.reject('stale_process')

    def test_native_list_tuple_thread_pair_equivalent(self):
        self.mocks['GetWindowThreadProcessId'].return_value = [7, 200]
        self.call()
        self.assertEqual(len(self.native.sent), 1)

    def test_capture_blocks_delivery_and_capture_read_failure_fails_closed(self):
        self.native.capture = 444
        self.reject('native_capture_active')
        self.native.capture = 0; self.native.capture_ok = False
        self.reject('capture_observation_failed')

    def test_capture_change_during_com_is_rechecked(self):
        self.adapter.view.on_find = lambda: setattr(self.native, 'capture', 444)
        self.reject('native_capture_active')

    def test_modifier_mouse_button_blocks_delivery(self):
        self.mocks['GetAsyncKeyState'].side_effect = lambda key: 0x8000 if key == 5 else 0
        self.reject('modifier_or_mouse_button_pressed')

    def test_dpi_checked_again_after_com(self):
        self.adapter.view.on_find = lambda: setattr(self.native, 'awareness', 0)
        self.reject('coordinate_context_unverified')

    def test_render_must_be_unique_same_process_and_exact_geometry(self):
        self.renders = [101, 102]
        self.reject('unverified_geometry')
        self.renders = [101]
        self.mocks['GetWindowThreadProcessId'].side_effect = lambda hwnd: (7, 201 if hwnd == 101 else 200)
        self.reject('unverified_geometry')
        self.mocks['GetWindowThreadProcessId'].side_effect = None
        self.mocks['GetClientRect'].return_value = (0, 0, 3240, 2039)
        self.reject('unverified_geometry')

    def test_render_handle_changed_during_com_rejected(self):
        self.adapter.view.on_find = lambda: setattr(self, 'renders', [102])
        self.reject('unverified_geometry')

    def test_coordinates_need_signed_range_and_exact_roundtrip(self):
        self.mocks['ScreenToClient'].return_value = (1948, 941)
        self.reject('coordinate_mapping_unverified')
        self.mocks['ScreenToClient'].return_value = POINT
        self.mocks['ClientToScreen'].return_value = (32768, 940)
        self.reject('coordinate_mapping_unverified')

    def test_coordinate_mapping_changed_during_com_rejected(self):
        self.adapter.view.on_find = lambda: setattr(self.mocks['ClientToScreen'], 'return_value', (-30035, -31060))
        self.reject('coordinate_mapping_unverified')

    def test_timeout_is_unknown_started_once_without_fallback(self):
        self.native.ok = False
        self.reject('wheel_result_unknown', started=True)

    def test_native_call_exception_is_started_even_without_return(self):
        self.native.on_send = Mock(side_effect=OSError('private native detail'))
        error = self.reject('wheel_result_unknown', started=True)
        self.assertNotIn('private native detail', str(error))

    def test_post_delivery_native_error_is_started(self):
        self.native.on_send = lambda: setattr(self.native, 'capture', 555)
        self.reject('native_capture_active', started=True)

    def test_post_delivery_provider_exception_is_started_and_sanitized(self):
        self.native.on_send = lambda: setattr(self.context, 'side_effect', RuntimeError('secret'))
        error = self.reject('history_wheel_observation_failed', started=True)
        self.assertNotIn('secret', str(error))


if __name__ == '__main__': unittest.main()
