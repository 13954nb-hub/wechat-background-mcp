"""Fake Win32-only contracts; no native window message is delivered."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import ExitStack

from wxbg import contact_wheel as cw
from wxbg.policy import AdapterError
from test_contact_actions import FakeAdapter


class Native:
    def __init__(self):
        self.sent = []; self.capture = 0; self.capture_ok = True
        self.awareness = 2; self.ok = True
    def GetGUIThreadInfo(self, thread, info):
        info._obj.hwndCapture = self.capture
        return self.capture_ok
    def GetThreadDpiAwarenessContext(self): return -3
    def GetAwarenessFromDpiAwarenessContext(self, context): return self.awareness
    def SendMessageTimeoutW(self, *args):
        self.sent.append(args[:-1])
        args[-1]._obj.value = 0
        return self.ok


class ContactWheelTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter()
        self.adapter.hwnd = 100; self.adapter.pid = 200; self.adapter.created = 300.0
        class CurrentTable:
            CurrentClassName = 'mmui::ContactsTableBaseView'
            def GetRuntimeId(inner): return self.adapter.table.element_info.runtime_id
            @property
            def CurrentBoundingRectangle(inner):
                return SimpleNamespace(**dict(zip(
                    ('left', 'top', 'right', 'bottom'), self.adapter.table.bounds)))
        self.current_nodes = [CurrentTable()]
        class Matches:
            @property
            def Length(inner): return len(self.current_nodes)
            def GetElement(inner, index): return self.current_nodes[index]
        self.adapter.root_node.info.element = SimpleNamespace(FindAll=lambda *_: Matches())
        self.adapter.client = SimpleNamespace(CreatePropertyCondition=lambda *args: args,
                                             CreateOrCondition=lambda *args: args)
        self.native = Native()
        stack = ExitStack(); self.addCleanup(stack.close)
        self.patches = {}
        specs = [
            (cw, 'USER32', self.native),
            (cw.psutil, 'Process', SimpleNamespace(create_time=lambda: 300.0)),
            (cw.win32process, 'GetWindowThreadProcessId', (7, 200)),
            (cw.win32api, 'GetAsyncKeyState', 0),
            (cw.win32gui, 'GetClassName', 'MMUIRenderSubWindowHW'),
            (cw.win32gui, 'GetClientRect', (0, 0, 3240, 2040)),
            (cw.win32gui, 'ClientToScreen', (-31576, -30885)),
            (cw.win32gui, 'ScreenToClient', (408, 1115)),
        ]
        for owner, name, result in specs:
            p = patch.object(owner, name, result) if name == 'USER32' else patch.object(owner, name, return_value=result)
            self.patches[name] = stack.enter_context(p)
        self.enum = stack.enter_context(patch.object(cw.win32gui, 'EnumChildWindows',
            side_effect=lambda main, callback, _: callback(101, None)))

    def call(self, delta=-120): cw.send_contact_wheel(self.adapter, self.adapter.table, delta)

    def use_current_measured_layout(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        self.patches['GetClientRect'].return_value = (0, 0, 3235, 1995)
        self.patches['ClientToScreen'].return_value = (-31576, -30908)
        self.patches['ScreenToClient'].return_value = (408, 1092)

    def test_current_measured_table_root_and_render_allow_one_wheel(self):
        self.use_current_measured_layout()
        self.call()
        self.assertEqual(len(self.native.sent), 1)
        self.patches['ClientToScreen'].assert_called_with(100, (408, 1092))

    def test_new_window_size_calculates_wheel_point_from_live_table(self):
        self.adapter.root_node.bounds = (300, 80, 1900, 1080)
        self.adapter.table.bounds = (380, 180, 760, 1000)
        self.patches['GetClientRect'].return_value = (0, 0, 1600, 1000)
        self.patches['ScreenToClient'].return_value = (270, 510)
        self.call()
        self.assertEqual(len(self.native.sent), 1)
        self.patches['ClientToScreen'].assert_any_call(100, (270, 510))

    def test_current_root_with_old_render_is_rejected_before_wheel(self):
        self.use_current_measured_layout()
        self.patches['GetClientRect'].return_value = (0, 0, 3240, 2040)
        self.reject('unverified_geometry')

    def test_old_root_with_current_table_is_rejected_before_wheel(self):
        self.use_current_measured_layout()
        self.adapter.table.bounds = (152, 200, 669, 1985)
        self.patches['ScreenToClient'].return_value = (408, 1092)
        # Resolution changes alone do not invalidate a newly measured table.
        self.call()
        self.assertEqual(len(self.native.sent), 1)

    def reject(self, code):
        with self.assertRaises(AdapterError) as caught: self.call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.native.sent, [])

    def test_one_finite_main_window_message_uses_signed_screen_coordinates(self):
        self.call()
        args = self.native.sent[0]
        self.assertEqual(args[:3], (100, 0x20a, (0xff88 << 16)))
        self.assertEqual(args[3], ((-30885 & 0xffff) << 16) | (-31576 & 0xffff))
        self.assertEqual(args[4:], (0x22, 1000))
        self.assertEqual(len(self.native.sent), 1)

    def test_timeout_has_no_repeat_or_child_fallback(self):
        self.native.ok = False
        with self.assertRaises(AdapterError) as caught: self.call()
        self.assertEqual(caught.exception.code, 'wheel_result_unknown')
        self.assertEqual(len(self.native.sent), 1)

    def test_inverse_uses_one_positive_notch(self):
        self.call(120)
        self.assertEqual(self.native.sent[0][2], 120 << 16)

    def test_native_pywin32_list_result_preserves_same_process_identity(self):
        self.patches['GetWindowThreadProcessId'].return_value = [7, 200]
        self.call()
        self.assertEqual(len(self.native.sent), 1)

    def test_only_discrete_known_deltas_are_accepted(self):
        for delta in (0, 1200, -1200, True, 120.0, '120'):
            with self.subTest(delta=delta), self.assertRaises(AdapterError): self.call(delta)
        self.assertEqual(self.native.sent, [])

    def test_recycled_main_window_pid_is_rejected(self):
        self.patches['GetWindowThreadProcessId'].return_value = (7, 201)
        self.reject('stale_process')

    def test_restarted_process_is_rejected(self):
        self.patches['Process'].return_value = SimpleNamespace(create_time=lambda: 301.0)
        self.reject('stale_process')

    def test_mouse_button_or_modifier_blocks_delivery(self):
        self.patches['GetAsyncKeyState'].side_effect = lambda key: 0x8000 if key == 5 else 0
        self.reject('modifier_or_mouse_button_pressed')

    def test_capture_and_capture_read_failure_block_delivery(self):
        self.native.capture = 99; self.reject('native_capture_active')
        self.native.capture = 0; self.native.capture_ok = False
        self.reject('capture_observation_failed')

    def test_unverified_dpi_context_is_rejected(self):
        self.native.awareness = 0
        self.reject('coordinate_context_unverified')

    def test_foreign_table_cannot_receive_scroll(self):
        self.adapter.table.info.class_name = 'OwnedOtherTable'
        self.reject('contacts_page_changed')

    def test_wrong_retained_root_geometry_is_rejected(self):
        self.adapter.root_node.bounds = (0, 0, 1296, 816)
        self.reject('contacts_page_changed')

    def test_render_surface_must_be_unique_with_known_geometry(self):
        self.enum.side_effect = lambda main, callback, _: [callback(h, None) for h in (101, 102)]
        self.reject('unverified_geometry')

    def test_roundtrip_and_signed_coordinate_range_are_mandatory(self):
        self.patches['ScreenToClient'].return_value = (407, 1114)
        self.reject('coordinate_mapping_unverified')
        self.patches['ScreenToClient'].return_value = (408, 1115)
        self.patches['ClientToScreen'].return_value = (40000, -30885)
        self.reject('coordinate_mapping_unverified')

    def test_precondition_failure_never_delivers_input(self):
        self.adapter.block_code = 'existing_popup'
        self.reject('existing_popup')

    def test_retained_table_is_not_enough_after_navigation_to_chat(self):
        bounds = SimpleNamespace(**dict(zip(('left', 'top', 'right', 'bottom'), self.adapter.table.bounds)))
        self.current_nodes = [SimpleNamespace(CurrentClassName='mmui::ChatInputField',
                                              CurrentBoundingRectangle=bounds,
                                              GetRuntimeId=lambda: ())]
        self.reject('contacts_page_changed')

    def test_table_plus_chat_input_or_no_table_is_rejected(self):
        self.current_nodes.append(SimpleNamespace(CurrentClassName='mmui::ChatInputField'))
        self.reject('contacts_page_changed')
        self.current_nodes = []
        self.reject('contacts_page_changed')

    def test_process_changed_during_uia_lookup_is_rejected_before_delivery(self):
        old = self.adapter.root_node.info.element.FindAll
        def change(*args):
            self.patches['GetWindowThreadProcessId'].return_value = (7, 201)
            return old(*args)
        self.adapter.root_node.info.element.FindAll = change
        self.reject('stale_process')

    def test_background_change_during_uia_lookup_is_rejected_before_delivery(self):
        old = self.adapter.root_node.info.element.FindAll
        def change(*args):
            self.adapter.block_code = 'background_requires_minimized'
            return old(*args)
        self.adapter.root_node.info.element.FindAll = change
        self.reject('background_requires_minimized')

    def test_layout_change_during_uia_lookup_is_rejected_before_delivery(self):
        old = self.adapter.root_node.info.element.FindAll
        def change(*args):
            self.adapter.root_node.bounds = (2, 0, 3237, 1995)
            return old(*args)
        self.adapter.root_node.info.element.FindAll = change
        self.reject('unverified_layout')

    def test_render_resize_during_uia_lookup_is_rejected_before_delivery(self):
        old = self.adapter.root_node.info.element.FindAll
        def change(*args):
            self.patches['GetClientRect'].return_value = (0, 0, 3235, 1995)
            return old(*args)
        self.adapter.root_node.info.element.FindAll = change
        self.reject('unverified_geometry')


if __name__ == '__main__': unittest.main()
