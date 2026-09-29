"""The session wheel rejects unbounded input before touching native APIs."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wxbg.policy import AdapterError


class SessionWheelTests(unittest.TestCase):
    def test_native_frame_uses_render_client_when_minimized_shell_collapses(self):
        from wxbg import session_wheel

        adapter = SimpleNamespace(hwnd=100, pid=9, created=1.0)
        geometry = {'root_rect': (520, 280, 2720, 1880),
                    'screen_point': (928, 1175)}

        def frame_with_clients(main_client, *, minimized=False, render_client=(0, 0, 2200, 1600)):
            def client_rect(hwnd):
                return render_client if hwnd == 101 else main_client

            with (patch.object(session_wheel, '_quiet'),
                  patch.object(session_wheel.psutil, 'Process',
                               return_value=SimpleNamespace(create_time=lambda: 1.0)),
                  patch.object(session_wheel.win32process, 'GetWindowThreadProcessId',
                               return_value=(7, 9)),
                  patch.object(session_wheel.win32gui, 'GetClassName',
                               side_effect=lambda hwnd: ('Qt51514QWindowIcon' if hwnd == 100
                                                        else 'MMUIRenderSubWindowHW')),
                  patch.object(session_wheel.win32gui, 'EnumChildWindows',
                               side_effect=lambda _hwnd, callback, _arg: callback(101, None)),
                  patch.object(session_wheel.win32gui, 'GetClientRect',
                               side_effect=client_rect),
                  patch.object(session_wheel.win32gui, 'IsIconic',
                               return_value=minimized, create=True),
                  patch.object(session_wheel.win32gui, 'ClientToScreen',
                               side_effect=lambda _hwnd, point:
                               (point[0] + 520, point[1] + 280)),
                  patch.object(session_wheel.win32gui, 'ScreenToClient',
                               side_effect=lambda _hwnd, point:
                               (point[0] - 520, point[1] - 280)),
                  patch.object(session_wheel.USER32, 'GetThreadDpiAwarenessContext',
                               return_value=1),
                  patch.object(session_wheel.USER32, 'GetAwarenessFromDpiAwarenessContext',
                               return_value=2)):
                return session_wheel._frame(adapter, geometry)

        self.assertEqual(frame_with_clients((0, 0, 2200, 1600))[3],
                         (928, 1175))
        with self.assertRaisesRegex(AdapterError, 'unverified_geometry'):
            frame_with_clients((0, 0, 2199, 1600))
        self.assertEqual(frame_with_clients((0, 0, 143, 18), minimized=True)[3],
                         (928, 1175))
        with self.assertRaisesRegex(AdapterError, 'unverified_geometry'):
            frame_with_clients((0, 0, 143, 18), minimized=True,
                               render_client=(0, 0, 2199, 1600))

    def test_live_uia_table_calibrates_wheel_point_after_resize(self):
        from wxbg.session_wheel import _current_table

        class Node:
            def __init__(self, kind, control, rect, runtime, parent=None):
                self.element_info = SimpleNamespace(
                    class_name=kind, control_type=control, runtime_id=runtime)
                self._rect = SimpleNamespace(left=rect[0], top=rect[1],
                                             right=rect[2], bottom=rect[3])
                self._parent = parent

            def rectangle(self):
                return self._rect

            def parent(self):
                return self._parent

        table_rect = (670, 480, 1187, 1870)
        root = Node('mmui::MainWindow', 'Window',
                    (520, 280, 2720, 1880), (11,))
        parent = Node('mmui::ChatSessionList', 'Group', table_rect,
                      (12,), root)
        table = Node('mmui::XTableView', 'List', table_rect, (13,), parent)
        raw_rect = SimpleNamespace(left=670, top=480, right=1187, bottom=1870)
        raw_table = SimpleNamespace(
            CurrentClassName='mmui::XTableView', CurrentControlType=50008,
            CurrentBoundingRectangle=raw_rect, GetRuntimeId=lambda: (13,))
        raw_parent = SimpleNamespace(
            CurrentClassName='mmui::ChatSessionList', CurrentControlType=50026,
            CurrentBoundingRectangle=raw_rect)
        root.element_info.element = SimpleNamespace(
            FindAll=lambda *_args: SimpleNamespace(
                Length=1, GetElement=lambda _index: raw_table))
        adapter = SimpleNamespace(
            root=lambda: root,
            client=SimpleNamespace(
                CreatePropertyCondition=lambda *_args: object(),
                ControlViewWalker=SimpleNamespace(
                    GetParentElement=lambda _node: raw_parent)))

        geometry = _current_table(adapter, table)
        self.assertEqual(geometry['root_rect'], (520, 280, 2720, 1880))
        self.assertEqual(geometry['table_rect'], table_rect)
        self.assertEqual(geometry['screen_point'], (928, 1175))

    def test_geometry_change_stops_before_wheel_delivery(self):
        from wxbg import session_wheel

        geometry = {'root_rect': (520, 280, 2720, 1880),
                    'table_rect': (670, 480, 1187, 1870),
                    'screen_point': (928, 1175),
                    'root_id': (11,), 'table_id': (13,)}
        changed = dict(geometry, table_rect=(670, 480, 1187, 1700))
        adapter = SimpleNamespace(precondition=lambda: None, hwnd=123)
        with (patch.object(session_wheel, '_current_table',
                           side_effect=[geometry, changed]),
              patch.object(session_wheel, '_frame', return_value=(1, 2, 3, 4)),
              patch.object(session_wheel.USER32, 'SendMessageTimeoutW') as send):
            with self.assertRaisesRegex(AdapterError, 'session_view_changed'):
                session_wheel.send_session_wheel(
                    adapter, object(), -120, check_context=lambda: None)
        send.assert_not_called()

    def test_invalid_wheel_delta_is_rejected_before_adapter_access(self):
        from wxbg.session_wheel import send_session_wheel

        class Adapter:
            def precondition(self):
                raise AssertionError('native boundary reached')

        for delta in (0, 240, -240, 'down'):
            with self.subTest(delta=delta):
                with self.assertRaisesRegex(AdapterError, 'invalid_session_wheel'):
                    send_session_wheel(Adapter(), object(), delta,
                                       check_context=lambda: None)

    def test_context_guard_is_required(self):
        from wxbg.session_wheel import send_session_wheel

        with self.assertRaisesRegex(AdapterError, 'missing_context_guard'):
            send_session_wheel(object(), object(), -120, check_context=None)


if __name__ == '__main__':
    unittest.main()
