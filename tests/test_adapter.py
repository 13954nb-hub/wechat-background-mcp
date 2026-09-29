"""Adapter contract tests with entirely fake UI objects and Windows modules.

Loading this module does not initialize COM, enumerate windows, or touch Weixin.
"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from wxbg.policy import AdapterError


def load_isolated_adapter():
    stubs = {name: types.ModuleType(name) for name in (
        'comtypes', 'comtypes.client', 'psutil', 'win32gui',
        'win32process', 'pywinauto', 'pywinauto.uia_defines', 'wxbg.monitor',
    )}
    stubs['comtypes'].client = stubs['comtypes.client']
    stubs['pywinauto'].Desktop = None
    stubs['pywinauto.uia_defines'].IUIA = None
    stubs['wxbg.monitor'].visible_windows = None
    source = Path(__file__).parents[1] / 'src' / 'wxbg' / 'adapter.py'
    spec = importlib.util.spec_from_file_location('wxbg._adapter_contract_test', source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    module.time = types.SimpleNamespace(sleep=lambda _: None)
    return module


adapter_module = load_isolated_adapter()


def node(runtime, name, kind, automation_id='', control_type='ListItem'):
    return types.SimpleNamespace(
        element_info=types.SimpleNamespace(
            runtime_id=runtime, name=name, class_name=kind,
            automation_id=automation_id, control_type=control_type,
        ),
        iface_selection_item=types.SimpleNamespace(CurrentIsSelected=False),
    )


def rect(left, top, right, bottom):
    return types.SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


class Value:
    def __init__(self, text=''):
        self.CurrentValue = text
        self.writes = []
        self.on_write = None

    def SetValue(self, text):
        self.writes.append(text)
        self.CurrentValue = text
        if self.on_write:
            self.on_write(text)


class FakeAdapter(adapter_module.Adapter):
    def __init__(self, draft='', submit='success', navigate='success'):
        self.identity = 'fake-process:123'
        self.submission_started = False
        self.chat = 'Alice'
        self.value = Value(draft)
        self.field_node = node('field', self.chat, 'FakeEdit', 'chat_input_field', 'Edit')
        self.field_node.iface_value = self.value
        self.session_nodes = [node('alice', 'Alice', 'mmui::ChatSessionCell', 'session_item_Alice')]
        self.session_nodes[0].iface_selection_item.CurrentIsSelected = True
        self.root_rect = (2, 0, 3237, 1995)
        self.send_rect = (3057, 1877, 3177, 1942)
        self.send_button = node('send', 'Send', 'mmui::XOutlineButton', control_type='Button')
        self.send_button.rectangle = lambda: rect(*self.send_rect)
        self.send_button.iface_invoke = None
        self.bubbles = []
        self.clicks = []
        self.submit = submit
        self.navigate = navigate
        self.preflight_error = None
        self.submission_path_calls = 0

    def preflight_click(self, target):
        if self.preflight_error:
            raise AdapterError(self.preflight_error)
        if target is self.send_button or target.element_info.class_name == 'mmui::XOutlineButton':
            left, top, right, bottom = self.send_rect
            root_left, root_top, _, _ = self.root_rect
            return (((top + bottom) // 2 - root_top) << 16) | ((left + right) // 2 - root_left)
        return (1909 << 16) | 3115

    def _post_click(self, _lp):
        self.submission_path_calls += 1
        self.click(self.send_button)

    def root(self):
        return types.SimpleNamespace(rectangle=lambda: rect(*self.root_rect))

    def precondition(self):
        pass

    def field(self):
        self.field_node.element_info.name = self.chat
        return self.field_node

    def nodes(self):
        self.field_node.element_info.name = self.chat
        return [self.field_node] + self.session_nodes + self.bubbles + ([self.send_button] if self.send_button else [])

    def click(self, target):
        self.clicks.append(target)
        if target.element_info.class_name == 'mmui::ChatSessionCell':
            self.chat = target.element_info.automation_id.removeprefix('session_item_')
            for session in self.session_nodes:
                session.iface_selection_item.CurrentIsSelected = (
                    session is target and self.navigate == 'success'
                )
            return
        if self.submit == 'timeout':
            return
        text = self.value.CurrentValue
        self.value.CurrentValue = ''
        if self.submit == 'switch_chat':
            self.chat = 'Bob'
            for session in self.session_nodes:
                session.iface_selection_item.CurrentIsSelected = False
        self.bubbles.append(node('new-bubble', text, 'mmui::ChatTextItemView'))

    def session_ref(self, index=0):
        return self.ref(self.session_nodes[index])

    def send_clicks(self):
        return [target for target in self.clicks if target is self.send_button]


class AdapterContractTests(unittest.TestCase):
    def test_visible_messages_exclude_rows_outside_current_message_list(self):
        adapter = object.__new__(adapter_module.Adapter)
        frame = node('frame', '', 'mmui::MessageView', control_type='Group')
        current_list = node('list', '', 'mmui::RecyclerListView', control_type='List')
        current_list.parent = lambda: frame
        current = node('current', 'current chat text', 'mmui::ChatTextItemView')
        current.parent = lambda: current_list
        unrelated = node('unrelated', 'another chat text', 'mmui::ChatTextItemView')
        unrelated.parent = lambda: None
        adapter.current_chat = lambda: 'Alice'
        adapter.nodes = lambda: [frame, current_list, current, unrelated]
        adapter.ref = lambda item, context='': 'ref-' + item.element_info.runtime_id

        result = adapter.messages(limit=50)
        self.assertEqual([item['text'] for item in result['messages']],
                         ['current chat text'])
        self.assertEqual({key: result['messages'][0][key] for key in (
            'sender_role', 'sender_role_verified', 'sender_role_evidence')}, {
                'sender_role': 'unknown', 'sender_role_verified': False,
                'sender_role_evidence': 'unavailable',
            })

    def test_visible_messages_classify_sender_from_unique_text_position(self):
        adapter = object.__new__(adapter_module.Adapter)
        frame = node('frame', '', 'mmui::MessageView', control_type='Group')
        frame.rectangle = lambda: rect(100, 100, 1100, 800)
        current_list = node('list', '', 'mmui::RecyclerListView', control_type='List')
        current_list.parent = lambda: frame
        rows = []
        for runtime_id, text, center_x in (
            ('incoming', 'incoming text', 200),
            ('outgoing', 'outgoing text', 1000),
            ('center', 'center text', 600),
        ):
            row = node(runtime_id, text, 'mmui::ChatTextItemView')
            row.parent = lambda current_list=current_list: current_list
            text_node = node(runtime_id + '-text', text, 'TextBlock',
                             control_type='Text')
            text_node.parent = lambda row=row: row
            text_node.rectangle = lambda center_x=center_x: rect(
                center_x - 20, 200, center_x + 20, 220)
            rows.extend((row, text_node))
        adapter.current_chat = lambda: 'Alice'
        adapter.root = lambda: types.SimpleNamespace(
            rectangle=lambda: rect(0, 0, 1200, 900))
        adapter.nodes = lambda: [frame, current_list] + rows
        adapter.ref = lambda item, context='': 'ref-' + item.element_info.runtime_id

        result = adapter.messages(limit=50)

        self.assertEqual(
            [(item['text'], item['sender_role'], item['sender_role_verified'],
              item['sender_role_evidence']) for item in result['messages']],
            [
                ('incoming text', 'other', True, 'live_ui_sender_side'),
                ('outgoing text', 'self', True, 'live_ui_sender_side'),
                ('center text', 'unknown', False, 'ambiguous'),
            ],
        )

    def test_send_at_username_submits_exact_literal_name(self):
        adapter = FakeAdapter()
        result = adapter.send_at_username(adapter.session_ref(), '寧', '')
        self.assertEqual(adapter.value.writes, ['@寧'])
        self.assertEqual(adapter.send_clicks(), [adapter.send_button])
        self.assertEqual(result['status'], 'submitted')

    def test_send_at_username_rejects_bad_name_before_draft_write(self):
        adapter = FakeAdapter()
        with self.assertRaisesRegex(AdapterError, 'invalid_username'):
            adapter.send_at_username(adapter.session_ref(), '@寧', '')
        self.assertEqual(adapter.value.writes, [])
        self.assertEqual(adapter.send_clicks(), [])

    def test_send_without_invoke_uses_one_target_local_click(self):
        adapter = FakeAdapter()
        adapter.send_button.iface_invoke = None
        result = adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(adapter.value.writes, ['probe'])
        self.assertEqual(adapter.value.CurrentValue, '')
        self.assertEqual(adapter.send_clicks(), [adapter.send_button])
        self.assertTrue(adapter.submission_started)
        self.assertEqual(adapter.submission_path_calls, 1)

    def test_send_uses_live_button_geometry_before_submission(self):
        adapter = FakeAdapter()
        adapter.send_rect = (3058, 1877, 3178, 1942)
        result = adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(adapter.send_clicks(), [adapter.send_button])
        self.assertTrue(adapter.submission_started)

    def test_send_rejects_preflight_point_mismatch_before_submission(self):
        adapter = FakeAdapter()
        adapter.preflight_click = lambda _target: (1909 << 16) | 3116
        with self.assertRaisesRegex(AdapterError, 'unverified_layout'):
            adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(adapter.value.writes, ['probe', ''])
        self.assertEqual(adapter.send_clicks(), [])
        self.assertFalse(adapter.submission_started)

    def test_send_accepts_measured_original_layout(self):
        adapter = FakeAdapter()
        adapter.root_rect = (0, 0, 3240, 2040)
        adapter.send_rect = (3060, 1922, 3180, 1987)
        adapter.preflight_click = lambda _target: (1954 << 16) | 3120
        result = adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(adapter.submission_path_calls, 1)

    def test_send_accepts_live_measured_window_height(self):
        adapter = FakeAdapter()
        adapter.root_rect = (0, 0, 3240, 1980)
        adapter.send_rect = (3060, 1862, 3180, 1927)
        adapter.preflight_click = lambda _target: (1894 << 16) | 3120
        result = adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(adapter.submission_path_calls, 1)

    def test_send_rejects_root_too_small_for_button_before_submission(self):
        adapter = FakeAdapter()
        adapter.root_rect = (2, 0, 500, 300)
        with self.assertRaisesRegex(AdapterError, 'unverified_layout'):
            adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(adapter.value.writes, ['probe', ''])
        self.assertEqual(adapter.send_clicks(), [])
        self.assertFalse(adapter.submission_started)

    def test_send_rechecks_button_geometry_after_preflight(self):
        adapter = FakeAdapter()
        preflight = adapter.preflight_click
        def move_button_during_preflight(target):
            adapter.send_rect = (3058, 1877, 3178, 1942)
            return preflight(target)
        adapter.preflight_click = move_button_during_preflight
        with self.assertRaisesRegex(AdapterError, 'unverified_layout'):
            adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(adapter.value.writes, ['probe', ''])
        self.assertEqual(adapter.send_clicks(), [])
        self.assertFalse(adapter.submission_started)

    def test_click_rechecks_preflight_before_posting(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.hwnd = 123
        adapter.preflight_click = lambda _target: (_ for _ in ()).throw(AdapterError('unverified_layout'))
        with self.assertRaisesRegex(AdapterError, 'unverified_layout'):
            adapter.click(object())

    def test_preflight_accepts_only_calibrated_current_render_mapping(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.hwnd = 123
        adapter.pid = 9
        adapter.precondition = lambda: None

        class Rect:
            left, top, right, bottom = 2, 0, 3237, 1995

            def width(self):
                return self.right - self.left

            def height(self):
                return self.bottom - self.top

        root = types.SimpleNamespace(rectangle=lambda: Rect())
        adapter.root = lambda: root
        owner = types.SimpleNamespace(handle=123)
        target = types.SimpleNamespace(handle=0,
            parent=lambda: owner,
            rectangle=lambda: types.SimpleNamespace(
                left=152, top=200, right=669, bottom=362))

        def children(_hwnd, callback, arg):
            callback(456, arg)

        with (patch.object(adapter_module.win32gui, 'EnumChildWindows', children, create=True),
              patch.object(adapter_module.win32gui, 'GetClassName',
                           lambda _hwnd: 'MMUIRenderSubWindowHW', create=True),
              patch.object(adapter_module.win32gui, 'GetClientRect',
                           lambda hwnd: ((0, 0, 143, 18) if hwnd == 123
                                         else (0, 0, 3235, 1995)), create=True),
              patch.object(adapter_module.win32gui, 'IsIconic', lambda _hwnd: True, create=True),
              patch.object(adapter_module.win32gui, 'ClientToScreen',
                           lambda _hwnd, point: (point[0] + 2, point[1]), create=True),
              patch.object(adapter_module.win32gui, 'ScreenToClient',
                           lambda _hwnd, point: (point[0] - 2, point[1]), create=True),
              patch.object(adapter_module.win32process,
                           'GetWindowThreadProcessId', lambda _hwnd: (1, 9), create=True)):
            self.assertEqual(adapter.preflight_click(target), (281 << 16) | 408)

    def test_preflight_accepts_minimized_window_with_shared_virtual_origin(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.hwnd = 123
        adapter.pid = 9
        adapter.precondition = lambda: None

        class Rect:
            left, top, right, bottom = 0, 0, 3240, 1980

            def width(self):
                return self.right - self.left

            def height(self):
                return self.bottom - self.top

        root = types.SimpleNamespace(rectangle=lambda: Rect())
        adapter.root = lambda: root
        owner = types.SimpleNamespace(handle=123)
        target = types.SimpleNamespace(handle=0,
            parent=lambda: owner,
            rectangle=lambda: types.SimpleNamespace(
                left=3060, top=1862, right=3180, bottom=1927))

        def children(_hwnd, callback, arg):
            callback(456, arg)

        def client_to_screen(_hwnd, point):
            return point[0] - 31984, point[1] - 32000

        with (patch.object(adapter_module.win32gui, 'EnumChildWindows', children, create=True),
              patch.object(adapter_module.win32gui, 'GetClassName',
                           lambda _hwnd: 'MMUIRenderSubWindowHW', create=True),
              patch.object(adapter_module.win32gui, 'GetClientRect',
                           lambda hwnd: ((0, 0, 359, 45) if hwnd == 123
                                         else (0, 0, 3240, 1980)), create=True),
              patch.object(adapter_module.win32gui, 'IsIconic', lambda _hwnd: True, create=True),
              patch.object(adapter_module.win32gui, 'ClientToScreen',
                           client_to_screen, create=True),
              patch.object(adapter_module.win32process,
                           'GetWindowThreadProcessId', lambda _hwnd: (1, 9), create=True)):
            self.assertEqual(adapter.preflight_click(target), (1894 << 16) | 3120)

    def test_preflight_still_rejects_nonminimized_origin_mismatch(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.hwnd = 123
        adapter.pid = 9
        adapter.precondition = lambda: None

        class Rect:
            left, top, right, bottom = 0, 0, 3240, 1980

            def width(self):
                return self.right - self.left

            def height(self):
                return self.bottom - self.top

        root = types.SimpleNamespace(rectangle=lambda: Rect())
        adapter.root = lambda: root
        owner = types.SimpleNamespace(handle=123)
        target = types.SimpleNamespace(handle=0,
            parent=lambda: owner,
            rectangle=lambda: types.SimpleNamespace(
                left=3060, top=1862, right=3180, bottom=1927))

        def children(_hwnd, callback, arg):
            callback(456, arg)

        def client_to_screen(_hwnd, point):
            return point[0] - 31984, point[1] - 32000

        with (patch.object(adapter_module.win32gui, 'EnumChildWindows', children, create=True),
              patch.object(adapter_module.win32gui, 'GetClassName',
                           lambda _hwnd: 'MMUIRenderSubWindowHW', create=True),
              patch.object(adapter_module.win32gui, 'GetClientRect',
                           lambda _hwnd: (0, 0, 3240, 1980), create=True),
              patch.object(adapter_module.win32gui, 'IsIconic', lambda _hwnd: False, create=True),
              patch.object(adapter_module.win32gui, 'ClientToScreen',
                           client_to_screen, create=True),
              patch.object(adapter_module.win32process,
                           'GetWindowThreadProcessId', lambda _hwnd: (1, 9), create=True)):
            with self.assertRaisesRegex(AdapterError, 'unverified_geometry'):
                adapter.preflight_click(target)

    def test_real_submission_preflight_failure_never_posts_or_marks_started(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.submission_started = False
        posted = []
        adapter.preflight_click = lambda _target: (_ for _ in ()).throw(AdapterError('unverified_layout'))
        adapter._post_click = posted.append
        with self.assertRaisesRegex(AdapterError, 'unverified_layout'):
            adapter.click_for_submission(object())
        self.assertEqual(posted, [])
        self.assertFalse(adapter.submission_started)

    def test_real_submission_marks_started_at_single_post_boundary(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.submission_started = False
        adapter.precondition = lambda: None
        posted = []
        adapter.preflight_click = lambda _target: 42
        adapter._post_click = lambda lp: posted.append((lp, adapter.submission_started))
        adapter.click_for_submission(object())
        self.assertEqual(posted, [(42, True)])

    def test_real_invoke_submits_once_without_geometry(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.submission_started = False
        adapter.precondition = lambda: None
        adapter.current_chat = lambda: 'Alice'
        adapter.selected_target = lambda _: True
        adapter.field = lambda: types.SimpleNamespace(iface_value=types.SimpleNamespace(CurrentValue='probe'))
        adapter._post_click = lambda _: self.fail('coordinate fallback used')
        calls = []
        button = node('send', 'Send', 'mmui::XOutlineButton', control_type='Button')
        button.iface_invoke = types.SimpleNamespace(Invoke=lambda: calls.append(adapter.submission_started))
        adapter.invoke_for_submission(button,'ref','Alice','probe')
        self.assertEqual(calls, [True])

    def test_real_invoke_unavailable_stays_before_submission_boundary(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.submission_started = False
        adapter.precondition = lambda: None
        adapter.current_chat = lambda: 'Alice'
        adapter.selected_target = lambda _: True
        adapter.field = lambda: types.SimpleNamespace(iface_value=types.SimpleNamespace(CurrentValue='probe'))
        button = node('send', 'Send', 'mmui::XOutlineButton', control_type='Button')
        button.iface_invoke = None
        with self.assertRaisesRegex(AdapterError, 'invoke_unavailable'):
            adapter.invoke_for_submission(button,'ref','Alice','probe')
        self.assertFalse(adapter.submission_started)

    def test_real_invoke_error_is_unknown_after_one_attempt(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.submission_started = False
        adapter.precondition = lambda: None
        adapter.current_chat = lambda: 'Alice'
        adapter.selected_target = lambda _: True
        adapter.field = lambda: types.SimpleNamespace(iface_value=types.SimpleNamespace(CurrentValue='probe'))
        calls = []
        button = node('send', 'Send', 'mmui::XOutlineButton', control_type='Button')
        def fail_once():
            calls.append(1)
            raise RuntimeError('provider failed after entry')
        button.iface_invoke = types.SimpleNamespace(Invoke=fail_once)
        with self.assertRaisesRegex(AdapterError, 'outcome_unknown'):
            adapter.invoke_for_submission(button,'ref','Alice','probe')
        self.assertEqual(calls, [1])
        self.assertTrue(adapter.submission_started)

    def test_click_rechecks_draft_at_final_boundary(self):
        adapter = FakeAdapter()
        preflight = adapter.preflight_click
        def change_draft_during_preflight(target):
            adapter.value.CurrentValue = 'changed by another actor'
            return preflight(target)
        adapter.preflight_click = change_draft_during_preflight
        with self.assertRaisesRegex(AdapterError, 'context_conflict'):
            adapter.send_text(adapter.session_ref(), 'probe')
        self.assertFalse(adapter.submission_started)
        self.assertEqual(adapter.send_clicks(), [])
        self.assertEqual(adapter.value.CurrentValue, 'changed by another actor')

    def test_shared_post_click_keeps_one_target_local_press_release_pair(self):
        adapter = object.__new__(adapter_module.Adapter)
        adapter.hwnd = 123
        posted = []
        with patch.object(adapter_module.win32gui, 'PostMessage', lambda *args: posted.append(args), create=True):
            adapter._post_click(42)
        self.assertEqual(posted, [(123, 0x201, 1, 42), (123, 0x202, 0, 42)])

    def test_failure_after_send_click_keeps_conservative_unknown(self):
        adapter = FakeAdapter()
        adapter.click = lambda _target: (_ for _ in ()).throw(AdapterError('unverified_layout'))
        with self.assertRaisesRegex(AdapterError, 'outcome_unknown'):
            adapter.send_text(adapter.session_ref(), 'probe')
        self.assertEqual(adapter.value.CurrentValue, 'probe')
        self.assertTrue(adapter.submission_started)
        self.assertEqual(adapter.submission_path_calls, 1)

    def test_existing_draft_is_preserved_without_submission(self):
        adapter = FakeAdapter(draft='user draft')
        with self.assertRaisesRegex(AdapterError, 'draft_conflict'):
            adapter.send_text(adapter.session_ref(), 'new message')
        self.assertEqual(adapter.value.CurrentValue, 'user draft')
        self.assertEqual(adapter.value.writes, [])
        self.assertEqual(adapter.send_clicks(), [])
        self.assertFalse(adapter.submission_started)

    def test_success_dispatches_send_button_exactly_once(self):
        adapter = FakeAdapter()
        result = adapter.send_text(adapter.session_ref(), 'hello')
        self.assertEqual(adapter.send_clicks(), [adapter.send_button])
        self.assertEqual(adapter.submission_path_calls, 1)
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertEqual(result['status'], 'submitted')
        self.assertNotIn('delivered', result['verification_level'])

    def test_session_preview_and_timestamp_changes_do_not_change_recipient_ref(self):
        adapter = FakeAdapter()
        target = adapter.session_nodes[0]
        ref = adapter.ref(target)
        target.element_info.name = 'Alice\nnew incoming message\n01:32\n1 unread'
        self.assertEqual(adapter.ref(target), ref)
        self.assertTrue(adapter.selected_target(ref))
        target.element_info.automation_id = 'session_item_Bob'
        self.assertNotEqual(adapter.ref(target), ref)
        self.assertFalse(adapter.selected_target(ref))

    def test_new_message_preview_after_send_preserves_exact_recipient_verification(self):
        adapter = FakeAdapter()
        click = adapter.click

        def send_with_preview_update(target):
            click(target)
            if target is adapter.send_button:
                adapter.session_nodes[0].element_info.name = 'Alice\nhello\n01:32\n'

        adapter.click = send_with_preview_update
        result = adapter.send_text(adapter.session_ref(), 'hello')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(adapter.send_clicks(), [adapter.send_button])

    def test_unknown_outcome_never_repeats_send_or_rewrites_draft(self):
        adapter = FakeAdapter(submit='timeout')
        with self.assertRaisesRegex(AdapterError, 'outcome_unknown'):
            adapter.send_text(adapter.session_ref(), 'hello')
        self.assertEqual(adapter.send_clicks(), [adapter.send_button])
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertTrue(adapter.submission_started)

    def test_failure_before_submission_restores_only_owned_draft(self):
        adapter = FakeAdapter()
        adapter.send_button = None
        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.session_ref(), 'hello')
        self.assertEqual(adapter.value.writes, ['hello', ''])
        self.assertEqual(adapter.send_clicks(), [])
        self.assertFalse(adapter.submission_started)

    def test_unknown_ref_cannot_fall_back_to_current_title(self):
        adapter = FakeAdapter()
        with self.assertRaises(AdapterError):
            adapter.open_session('unknown-ref')
        self.assertEqual(adapter.clicks, [])

    def test_unique_ref_for_different_title_opens_that_session(self):
        adapter = FakeAdapter()
        bob = node('bob', 'Bob', 'mmui::ChatSessionCell', 'session_item_Bob')
        adapter.session_nodes.append(bob)
        result = adapter.open_session(adapter.ref(bob))
        self.assertEqual(adapter.clicks, [bob])
        self.assertEqual(result['title'], 'Bob')
        self.assertTrue(bob.iface_selection_item.CurrentIsSelected)
        self.assertEqual(adapter.navigation_evidence, {
            'session_ref': adapter.ref(bob), 'title': 'Bob',
            'activation_started': True, 'selected_and_header_verified': True,
        })

    def test_same_title_still_navigates_to_the_requested_ref(self):
        adapter = FakeAdapter()
        target = adapter.session_nodes[0]
        # Another unexposed recipient may share this displayed title.
        target.iface_selection_item.CurrentIsSelected = False
        result = adapter.open_session(adapter.ref(target))
        self.assertEqual(adapter.clicks, [target])
        self.assertTrue(target.iface_selection_item.CurrentIsSelected)
        self.assertEqual(result['title'], 'Alice')

    def test_exact_selected_ref_and_header_does_not_toggle_selection(self):
        adapter = FakeAdapter(navigate='no_selection')
        result = adapter.open_session(adapter.session_ref())
        self.assertEqual(adapter.clicks, [])
        self.assertTrue(adapter.session_nodes[0].iface_selection_item.CurrentIsSelected)
        self.assertEqual(result['title'], 'Alice')
        self.assertFalse(adapter.navigation_evidence['activation_started'])
        self.assertTrue(adapter.navigation_evidence['selected_and_header_verified'])

    def test_matching_selected_ref_without_matching_header_still_navigates(self):
        adapter = FakeAdapter()
        adapter.chat = 'Bob'
        result = adapter.open_session(adapter.session_ref())
        self.assertEqual(adapter.clicks, [adapter.session_nodes[0]])
        self.assertEqual(result['title'], 'Alice')

    def test_multiple_selected_rows_do_not_use_header_shortcut(self):
        adapter = FakeAdapter(navigate='no_selection')
        bob = node('bob', 'Bob', 'mmui::ChatSessionCell', 'session_item_Bob')
        bob.iface_selection_item.CurrentIsSelected = True
        adapter.session_nodes.append(bob)
        with self.assertRaises(AdapterError):
            adapter.open_session(adapter.session_ref())
        self.assertEqual(adapter.clicks, [adapter.session_nodes[0]])
        self.assertTrue(adapter.navigation_evidence['activation_started'])
        self.assertFalse(adapter.navigation_evidence['selected_and_header_verified'])

    def test_matching_header_without_selected_target_cannot_send(self):
        adapter = FakeAdapter(navigate='no_selection')
        adapter.session_nodes[0].iface_selection_item.CurrentIsSelected = False
        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.session_ref(), 'hello')
        self.assertEqual(adapter.send_clicks(), [])
        self.assertEqual(adapter.value.writes, [])
        self.assertFalse(adapter.submission_started)

    def test_selection_lost_before_submit_cannot_send_or_clear_another_draft(self):
        adapter = FakeAdapter()
        target = adapter.session_nodes[0]

        def switch_selected_recipient(_text):
            # Same displayed header; the requested recipient is no longer selected.
            target.iface_selection_item.CurrentIsSelected = False

        adapter.value.on_write = switch_selected_recipient
        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.session_ref(), 'hello')
        self.assertEqual(adapter.send_clicks(), [])
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertFalse(adapter.submission_started)

    def test_duplicate_titles_fail_closed_even_with_unique_ref(self):
        adapter = FakeAdapter()
        other_alice = node('other-alice', 'Alice', 'mmui::ChatSessionCell', 'session_item_Alice')
        adapter.session_nodes.append(other_alice)
        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.ref(other_alice), 'hello')
        self.assertEqual(adapter.value.writes, [])
        self.assertEqual(adapter.clicks, [])

    def test_chat_switch_cannot_prove_original_submission(self):
        adapter = FakeAdapter(submit='switch_chat')
        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.session_ref(), 'hello')
        self.assertEqual(adapter.send_clicks(), [adapter.send_button])
        self.assertTrue(adapter.submission_started)

    def test_payment_named_session_is_rejected_before_writing(self):
        adapter = FakeAdapter()
        payment = node('pay', '微信支付', 'mmui::ChatSessionCell', 'session_item_微信支付')
        adapter.session_nodes.append(payment)
        with self.assertRaisesRegex(AdapterError, 'payment_excluded'):
            adapter.send_text(adapter.ref(payment), 'hello')
        self.assertEqual(adapter.value.writes, [])
        self.assertEqual(adapter.clicks, [])


if __name__ == '__main__':
    unittest.main()
