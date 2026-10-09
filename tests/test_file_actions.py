"""Fake-only tests. No Windows API, Weixin, filesystem fixture, or real driver."""
import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg.policy import AdapterError, exact_one
from wxbg import file_actions

FIXTURE = {'path': r'C:\owned\fixture.txt', 'name': 'fixture.txt', 'size': 137, 'sha256': 'a' * 64}
EVIDENCE = {'matches': 1, 'accepted_shows': 1, 'live': 0, 'installed': 0,
            'active_filter': 0, 'protection_restored': 1, 'cleanup_unresolved': 0,
            'released': True, 'grant_revoked': True}


class Node:
    def __init__(self, cls, name, rid, control='ListItem', aid='', rect=(0, 0, 1, 1)):
        self._element_info = SimpleNamespace(class_name=cls, name=name, runtime_id=rid,
                                             control_type=control, automation_id=aid)
        self.bounds = rect
        self.stale = False
        self.on_rectangle = None

    @property
    def element_info(self):
        if self.stale:
            raise RuntimeError('owned stale UIA element')
        return self._element_info

    def rectangle(self):
        if self.stale:
            raise RuntimeError('owned stale UIA rectangle')
        if self.on_rectangle:
            self.on_rectangle()
        return SimpleNamespace(**dict(zip(('left', 'top', 'right', 'bottom'), self.bounds)))


class SelectionValue:
    def __init__(self, adapter):
        self.adapter = adapter

    @property
    def CurrentIsSelected(self):
        if self.adapter.selection_error:
            raise RuntimeError('owned SelectionItem provider failure')
        return self.adapter.selected


class FieldValue:
    def __init__(self, adapter):
        self.adapter = adapter

    @property
    def CurrentValue(self):
        if self.adapter.value_error:
            raise RuntimeError('owned Value provider failure')
        if self.adapter.read_draft_hook:
            self.adapter.read_draft_hook(self.adapter)
        return self.adapter.text

    def SetValue(self, value):
        self.adapter.set_value_calls += 1
        raise AssertionError('File drafts must never use SetValue')


def card(rid='new-card', name='fixture.txt', state='正在上傳', percent='0%'):
    return Node('mmui::ChatBubbleItemView', f'檔案\n進度: {percent}\n{name}\n137B\n{state}\n微信电脑版', rid)


class FakeAdapter:
    def __init__(self):
        self.submission_started = False
        self.title = '檔案傳輸'
        self.text = ''
        self.session = Node('mmui::ChatSessionCell', '檔案傳輸\nold preview', 'session-one', aid='session_item_檔案傳輸')
        self.session.iface_selection_item = SelectionValue(self)
        self.input = Node('mmui::ChatInputField', self.title, 'input-one', control='Edit', aid='chat_input_field')
        self.input.iface_value = FieldValue(self)
        self.attachment = Node('mmui::XButton', '傳送檔案', 'attachment-one', control='Button', rect=(887, 1920, 957, 1990))
        self.send = Node('mmui::XOutlineButton', '傳送', 'send-one', control='Button', rect=(3060, 1922, 3180, 1987))
        self.attachment.on_rectangle = lambda: self.on_attachment_lookup(self) if self.on_attachment_lookup else None
        self.send.on_rectangle = lambda: self.on_send_lookup(self) if self.on_send_lookup else None
        self.root_node = Node('Root', '', 'root', rect=(0, 0, 3240, 2040))
        self.message_nodes = []
        self.extra_nodes = []
        self.selected = True
        self.selection_error = False
        self.value_error = False
        self.clicks = 0
        self.open_calls = []
        self.nodes_calls = 0
        self.set_value_calls = 0
        self.preflight_calls = []
        self.preflight_override = None
        self.on_send_lookup = None
        self.on_attachment_lookup = None
        self.on_click = None
        self.read_draft_hook = None

    def precondition(self):
        pass

    def root(self):
        return self.root_node

    def nodes(self):
        self.nodes_calls += 1
        self.input._element_info.name = self.title
        return [self.session, self.input, self.attachment, self.send, *self.message_nodes, *self.extra_nodes]

    def ref(self, node, context=''):
        return context + ':' + str(node.element_info.runtime_id)

    def selected_target(self, ref):
        # Mirror Adapter.selected_target's full descendant walk, even though
        # this fake has only one session and a simple selected flag.
        rows = [n for n in self.nodes() if n.element_info.class_name == 'mmui::ChatSessionCell']
        matches = [n for n in rows if self.ref(n) == ref]
        return self.selected and len(matches) == 1

    def open_session(self, ref):
        self.open_calls.append(ref)
        rows = [n for n in self.nodes() if n.element_info.class_name == 'mmui::ChatSessionCell']
        if len([n for n in rows if self.ref(n) == ref]) != 1:
            raise AdapterError('not_found')
        # Same already-selected fast path as the real adapter: its selected
        # target and current_chat checks each trigger their own full walk.
        already_open = self.selected_target(ref) and self.current_chat() == self.title
        if not already_open:
            self.selected = True
        return {'title': self.title, 'status': 'opened'}

    def current_chat(self):
        return self.field().element_info.name

    def field(self):
        self.input.element_info.name = self.title
        return self.one(lambda i: i.automation_id == 'chat_input_field', 'chat_input')

    def draft(self):
        field = self.field()
        return {'chat': field.element_info.name, 'text': field.iface_value.CurrentValue}

    def one(self, predicate, subject):
        return exact_one([n for n in self.nodes() if predicate(n.element_info)], subject)

    def click(self, node):
        if node is not self.send:
            raise AssertionError('Driver owns attachment activation; adapter only clicks Send')
        self.clicks += 1
        if self.on_click:
            self.on_click(self)
        else:
            self.text = ''
            self.message_nodes.append(card())

    def preflight_click(self, node):
        self.preflight_calls.append(node)
        if self.preflight_override is not None:
            return self.preflight_override
        left, top, _, _ = self.root_node.bounds
        x1, y1, x2, y2 = node.bounds
        x = (x1 + x2) // 2 - left
        y = (y1 + y2) // 2 - top
        return (y << 16) | x


class FakeNative:
    def __init__(self, adapter):
        self.adapter = adapter
        self.calls = 0
        self.points = []
        self.evidence = copy.deepcopy(EVIDENCE)
        self.after_select = None
        self.error = None

    def select_file(self, fixture, *, attachment_point=(922, 1955), attachment_size=None):
        assert self.adapter.submission_started, 'Must mark before possible native side effect'
        assert fixture == FIXTURE
        self.calls += 1
        self.points.append(attachment_point)
        if self.error:
            raise self.error
        self.adapter.text = '\ufffc'
        if self.after_select:
            self.after_select(self.adapter)
        return copy.deepcopy(self.evidence)


class FileActionsTests(unittest.TestCase):
    def setUp(self):
        self.reset_state()
        self.sleep_patch = patch.object(file_actions.time, 'sleep', return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def reset_state(self):
        self.adapter = FakeAdapter()
        self.driver = FakeNative(self.adapter)
        self.ref = self.adapter.ref(self.adapter.session)

    def run_action(self):
        return file_actions.send_file(self.adapter, self.driver, self.ref, copy.deepcopy(FIXTURE))

    def assert_unknown(self):
        with self.assertRaises(AdapterError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, 'outcome_unknown')
        self.assertTrue(self.adapter.submission_started)
        self.assertEqual(self.adapter.set_value_calls, 0)

    def test_exact_success_is_local_submission_uploading_not_receipt(self):
        result = self.run_action()
        self.assertEqual(result['status'], 'submitted')
        self.assertTrue(result['submitted'])
        self.assertEqual(result['verification_level'], 'new_local_attachment_card_and_empty_draft')
        self.assertEqual(result['upload_status'], 'uploading')
        self.assertFalse(result['remote_receipt_verified'])
        self.assertEqual(result['counts']['submitted'], 1)
        self.assertEqual(self.adapter.open_calls, [self.ref])
        self.assertEqual((self.driver.calls, self.adapter.clicks, self.adapter.set_value_calls), (1, 1, 0))
        print(f'FAKE_TREE_WALK_BASELINE happy_path_nodes_calls={self.adapter.nodes_calls}', flush=True)
        self.assertLessEqual(self.adapter.nodes_calls, 15)

    def test_exact_simplified_attachment_and_send_labels(self):
        self.adapter.attachment._element_info.name = '发送文件'
        self.adapter.send._element_info.name = '发送'
        result = self.run_action()
        self.assertTrue(result['submitted'])
        self.assertEqual((self.driver.calls, self.adapter.clicks), (1, 1))
        self.assertFalse(result['remote_receipt_verified'])

    def test_simplified_file_card_is_observed_after_submission(self):
        def simplified_card(adapter):
            adapter.text = ''
            adapter.message_nodes = [Node('mmui::ChatBubbleItemView',
                '文件\nfixture.txt\n137B', 'simplified-card')]
        self.adapter.on_click = simplified_card
        result = self.run_action()
        self.assertTrue(result['submitted'])
        self.assertEqual(result['upload_status'], 'unknown')
        self.assertFalse(result['remote_receipt_verified'])

    def test_unknown_localized_attachment_label_stops_before_selection(self):
        self.adapter.attachment._element_info.name = '发送文件到其他聊天'
        with self.assertRaises(AdapterError):
            self.run_action()
        self.assertEqual((self.driver.calls, self.adapter.clicks), (0, 0))

    def test_duplicate_localized_attachment_labels_stop_before_selection(self):
        self.adapter.extra_nodes.append(Node('mmui::XButton', '发送文件',
            'duplicate-attachment', control='Button', rect=(887, 1920, 957, 1990)))
        with self.assertRaises(AdapterError):
            self.run_action()
        self.assertEqual((self.driver.calls, self.adapter.clicks), (0, 0))

    def test_file_send_phase_marks_only_observed_success_milestones(self):
        self.run_action()
        self.assertEqual(getattr(self.adapter, 'file_send_phase', None), {
            'native_selection_started': True,
            'native_selection_completed': True,
            'embedded_file_staged': True,
            'send_click_attempted': True,
            'local_card_observed': True,
        })

    def test_file_send_phase_retains_native_start_when_driver_raises(self):
        self.driver.error = TimeoutError('private native failure')
        self.assert_unknown()
        self.assertEqual(getattr(self.adapter, 'file_send_phase', None), {
            'native_selection_started': True,
            'native_selection_completed': False,
            'embedded_file_staged': False,
            'send_click_attempted': False,
            'local_card_observed': False,
        })

    def test_file_send_phase_marks_click_attempt_before_click_exception(self):
        self.adapter.on_click = lambda _: (_ for _ in ()).throw(RuntimeError('private click failure'))
        self.assert_unknown()
        self.assertEqual(getattr(self.adapter, 'file_send_phase', None), {
            'native_selection_started': True,
            'native_selection_completed': True,
            'embedded_file_staged': True,
            'send_click_attempted': True,
            'local_card_observed': False,
        })

    def test_file_send_phase_records_native_card_without_second_send(self):
        self.driver.after_select = lambda adapter: adapter.message_nodes.append(card())
        self.assert_unknown()
        self.assertEqual(self.adapter.file_send_phase, {
            'native_selection_started': True,
            'native_selection_completed': True,
            'embedded_file_staged': True,
            'send_click_attempted': False,
            'local_card_observed': True,
        })
        self.assertEqual(self.adapter.clicks, 0)

    def test_existing_draft_is_preserved_before_any_selection(self):
        self.adapter.text = 'existing text'
        with self.assertRaises(AdapterError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, 'draft_conflict')
        self.assertFalse(self.adapter.submission_started)
        self.assertEqual((self.driver.calls, self.adapter.clicks), (0, 0))
        self.assertEqual(self.adapter.text, 'existing text')

    def test_wrong_ref_rejected_before_action(self):
        self.ref = 'wrong-ref'
        with self.assertRaises(AdapterError):
            self.run_action()
        self.assertFalse(self.adapter.submission_started)
        self.assertEqual(self.driver.calls, 0)

    def test_unverified_attachment_or_root_geometry_never_activates(self):
        for target in ('attachment', 'root_node'):
            with self.subTest(target=target):
                self.reset_state()
                getattr(self.adapter, target).bounds = (1, 2, 3, 4)
                with self.assertRaises(AdapterError) as raised:
                    self.run_action()
                self.assertEqual(raised.exception.code, 'unverified_layout')
                self.assertFalse(self.adapter.submission_started)
                self.assertEqual(self.driver.calls, 0)

    def test_measured_root_and_button_bounds_support_dynamic_window_size(self):
        # Coordinates are measured from this live window; no fixed-resolution
        # preset should be required when all semantic bounds are verified.
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.attachment.bounds = (889, 1875, 959, 1945)
        self.adapter.send.bounds = (3057, 1877, 3177, 1942)

        result = self.run_action()
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.driver.points, [(922, 1910)])
        self.assertEqual((self.driver.calls, self.adapter.clicks), (1, 1))

    def test_measured_new_layout_uses_exact_native_attachment_point(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.attachment.bounds = (890, 1875, 960, 1945)
        self.adapter.send.bounds = (3057, 1877, 3177, 1942)

        result = self.run_action()

        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.driver.points, [(923, 1910)])
        self.assertGreaterEqual(self.adapter.preflight_calls.count(self.adapter.attachment), 2)
        self.assertIn(self.adapter.send, self.adapter.preflight_calls)

    def test_measured_layout_rejects_preflight_point_mismatch_before_native(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.attachment.bounds = (890, 1875, 960, 1945)
        self.adapter.send.bounds = (3057, 1877, 3177, 1942)
        self.adapter.preflight_override = (1910 << 16) | 922

        with self.assertRaises(AdapterError) as raised:
            self.run_action()

        self.assertEqual(raised.exception.code, 'unverified_layout')
        self.assertFalse(self.adapter.submission_started)
        self.assertEqual((self.driver.calls, self.adapter.clicks), (0, 0))

    def test_attachment_name_must_be_exact(self):
        self.adapter.attachment.element_info.name = '傳送檔案 extra'
        with self.assertRaises(AdapterError):
            self.run_action()
        self.assertFalse(self.adapter.submission_started)
        self.assertEqual(self.driver.calls, 0)

    def test_missing_or_bad_native_evidence_never_clicks_send(self):
        for key, value in EVIDENCE.items():
            with self.subTest(key=key):
                self.reset_state()
                del self.driver.evidence[key]
                self.assert_unknown()
                self.assertEqual((self.driver.calls, self.adapter.clicks), (1, 0))
        for key, value in [('accepted_shows', 0), ('accepted_shows', 2), ('live', 1), ('installed', 1), ('active_filter', 1), ('grant_revoked', False)]:
            with self.subTest(key=key, value=value):
                self.reset_state()
                self.driver.evidence[key] = value
                self.assert_unknown()
                self.assertEqual(self.adapter.clicks, 0)

    def test_native_exception_is_sticky_unknown_without_send(self):
        self.driver.error = TimeoutError('owned mock timeout')
        self.assert_unknown()
        self.assertEqual((self.driver.calls, self.adapter.clicks), (1, 0))
        self.assert_unknown()
        self.assertEqual((self.driver.calls, self.adapter.clicks), (1, 0))

    def test_context_changed_by_selection_never_sends(self):
        self.driver.after_select = lambda a: setattr(a, 'title', 'different recipient')
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 0)

    def test_field_replacement_after_selection_is_unknown(self):
        self.driver.after_select = lambda a: setattr(a.input.element_info, 'runtime_id', 'replacement-field')
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 0)

    def test_requires_exactly_one_embedded_object(self):
        for text in ('', '\ufffc\ufffc', 'text\ufffc'):
            with self.subTest(text=text):
                self.reset_state()
                self.driver.after_select = lambda a, text=text: setattr(a, 'text', text)
                self.assert_unknown()
                self.assertEqual(self.adapter.clicks, 0)

    def test_context_or_draft_change_during_send_lookup_stops_click(self):
        for change, expected in ((lambda a: setattr(a, 'selected', False), 'context_conflict'),
                                 (lambda a: setattr(a, 'text', '\ufffcchanged'), 'draft_conflict')):
            self.reset_state()
            self.adapter.on_send_lookup = change
            with self.assertRaises(AdapterError) as raised:
                self.run_action()
            self.assertEqual(raised.exception.code, expected)
            self.assertFalse(self.adapter.submission_started)
            self.assertEqual((self.driver.calls, self.adapter.clicks), (0, 0))
            self.assertEqual(self.adapter.clicks, 0)

    def test_native_autotransmit_card_does_not_trigger_second_send(self):
        self.driver.after_select = lambda a: a.message_nodes.append(card())
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 0)

    def test_send_exception_never_retries(self):
        def fail(_):
            raise RuntimeError('dispatch may have happened')
        self.adapter.on_click = fail
        self.assert_unknown()
        self.assertEqual((self.driver.calls, self.adapter.clicks), (1, 1))
        self.assert_unknown()
        self.assertEqual((self.driver.calls, self.adapter.clicks), (1, 1))

    def test_context_change_after_send_is_unknown(self):
        self.adapter.on_click = lambda a: setattr(a, 'title', 'changed after click')
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 1)

    def test_two_new_matching_cards_are_ambiguous_and_not_retried(self):
        def duplicate(a):
            a.text = ''
            a.message_nodes.extend([card('one'), card('two')])
        self.adapter.on_click = duplicate
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 1)

    def test_old_card_preview_change_is_not_new_message(self):
        old = card('old-card', name='old-name.txt')
        self.adapter.message_nodes.append(old)
        def update(a):
            a.text = ''
            old.element_info.name = card().element_info.name
        self.adapter.on_click = update
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 1)

    def test_session_preview_changes_do_not_change_recipient_ref(self):
        self.driver.after_select = lambda a: setattr(a.session.element_info, 'name', '檔案傳輸\nnew preview')
        self.assertEqual(self.run_action()['status'], 'submitted')
        self.assertEqual(self.adapter.clicks, 1)

    def test_similar_filename_is_not_exact_file_card(self):
        def wrong_name(a):
            a.text = ''
            a.message_nodes.append(card(name='prefix-fixture.txt'))
        self.adapter.on_click = wrong_name
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 1)

    def test_upload_failure_and_unknown_are_reported_without_resend(self):
        for label, expected in [('傳送中斷', 'interrupted'), ('发送失败', 'failed'), ('傳送失敗', 'failed'), ('', 'unknown'), ('上傳完成', 'completed')]:
            with self.subTest(label=label):
                self.reset_state()
                def append(a, label=label):
                    a.text = ''
                    a.message_nodes.append(card(state=label, percent='0%' if label != '上傳完成' else '100%'))
                self.adapter.on_click = append
                result = self.run_action()
                self.assertTrue(result['submitted'])
                self.assertEqual(result['upload_status'], expected)
                self.assertFalse(result['remote_receipt_verified'])
                self.assertEqual(self.adapter.clicks, 1)

    def test_existing_card_same_name_does_not_prevent_one_new_card(self):
        self.adapter.message_nodes.append(card('old-card'))
        result = self.run_action()
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(self.adapter.clicks, 1)

    def test_draft_change_during_last_attachment_lookup_stops_before_native(self):
        calls = 0
        def change_on_second(a):
            nonlocal calls
            calls += 1
            if calls == 2:
                a.text = 'new user draft'
        self.adapter.on_attachment_lookup = change_on_second
        with self.assertRaises(AdapterError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, 'draft_conflict')
        self.assertFalse(self.adapter.submission_started)
        self.assertEqual((self.driver.calls, self.adapter.clicks), (0, 0))
        self.assertEqual(self.adapter.text, 'new user draft')

    def test_status_words_in_filename_do_not_claim_completed_upload(self):
        with patch.dict(FIXTURE, {'name': '上傳完成', 'path': 'C:\\owned\\上傳完成'}):
            def append(a):
                a.text = ''
                a.message_nodes.append(card(name=FIXTURE['name'], state=''))
            self.adapter.on_click = append
            self.assertEqual(self.run_action()['upload_status'], 'unknown')

    def test_unrecognized_status_phrase_does_not_claim_completed_upload(self):
        def append(a):
            a.text = ''
            a.message_nodes.append(card(state='沒有上傳完成標記'))
        self.adapter.on_click = append
        self.assertEqual(self.run_action()['upload_status'], 'unknown')

    def test_text_bubble_with_filename_is_not_an_attachment_card(self):
        def append(a):
            a.text = ''
            node = card()
            node.element_info.class_name = 'mmui::ChatTextItemView'
            a.message_nodes.append(node)
        self.adapter.on_click = append
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 1)

    def test_selection_provider_error_before_selection_rejects(self):
        self.adapter.selection_error = True
        with self.assertRaises(AdapterError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, 'observation_failed')
        self.assertFalse(self.adapter.submission_started)
        self.assertEqual(self.driver.calls, 0)

    def test_selection_provider_error_after_native_is_unknown(self):
        self.driver.after_select = lambda a: setattr(a, 'selection_error', True)
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 0)

    def test_stale_field_after_native_is_unknown(self):
        self.driver.after_select = lambda a: setattr(a.input, 'stale', True)
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 0)

    def test_value_provider_error_after_native_is_unknown(self):
        self.driver.after_select = lambda a: setattr(a, 'value_error', True)
        self.assert_unknown()
        self.assertEqual(self.adapter.clicks, 0)

    def test_multiple_selected_rows_fail_even_if_target_selected(self):
        row = Node('mmui::ChatSessionCell', 'other', 'other-session', aid='session_item_other')
        row.iface_selection_item = SimpleNamespace(CurrentIsSelected=True)
        self.adapter.extra_nodes.append(row)
        with self.assertRaises(AdapterError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, 'context_conflict')
        self.assertFalse(self.adapter.submission_started)
        self.assertEqual(self.driver.calls, 0)

    def test_native_failure_retains_primary_and_cleanup_evidence(self):
        evidence = {'passed': False, 'primary_error': {'code': 'native_selection_timeout'},
                    'cleanup_errors': [{'stage': 'restore', 'code': 'native_cleanup_unverified'}]}
        failure = AdapterError('outcome_unknown')
        failure.evidence = evidence
        with patch.object(self.driver, 'select_file', side_effect=failure):
            with self.assertRaises(AdapterError) as caught:
                self.run_action()
        self.assertEqual(caught.exception.code, 'outcome_unknown')
        self.assertEqual(caught.exception.evidence, evidence)
        self.assertEqual(self.adapter.native_evidence, evidence)
        self.assertEqual(self.adapter.clicks, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
