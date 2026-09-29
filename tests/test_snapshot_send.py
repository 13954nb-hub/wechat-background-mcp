"""Synthetic checks for fresh send snapshots and one guarded local click."""
import unittest

from test_adapter import FakeAdapter, node
from wxbg.policy import AdapterError


class SnapshotAdapter(FakeAdapter):
    def __init__(self, *, duplicate_field_after_write=False, race_after_write=False,
                 mutate_button_during_preflight=False, submit='success',
                 duplicate_field_after_click=False, race_after_click=False,
                 omit_field_after_write=False, replace_field_after_write=False,
                 reuse_field_runtime_id_on_replace=False):
        super().__init__(submit=submit)
        self.tree_calls = 0
        self.click_tree_calls = []
        self.click_generations = []
        self.precondition_count = 0
        self.preconditions_at_click = []
        self.duplicate_field_after_write = duplicate_field_after_write
        self.race_after_write = race_after_write
        self.mutate_button_during_preflight = mutate_button_during_preflight
        self.duplicate_field_after_click = duplicate_field_after_click
        self.race_after_click = race_after_click
        self.omit_field_after_write = omit_field_after_write
        self.replace_field_after_write = replace_field_after_write
        self.reuse_field_runtime_id_on_replace = reuse_field_runtime_id_on_replace
        self.original_field = self.field_node
        self.replacement_field = None
        self.replacement_value = None
        self.race_armed = False
        self.pending_button = None
        self.value.on_write = self._after_write

    def _after_write(self, text):
        if text:
            self.race_armed = self.race_after_write
            if self.replace_field_after_write:
                self.replacement_value = self.value.__class__(text)
                self.replacement_field = node(
                    (self.original_field.element_info.runtime_id
                     if self.reuse_field_runtime_id_on_replace else 'replacement-field'),
                    self.chat, 'mmui::ChatInputField',
                    'chat_input_field', 'Edit',
                )
                self.replacement_field.iface_value = self.replacement_value
                self.field_node = self.replacement_field
                self.send_button = False

    def precondition(self):
        self.precondition_count += 1

    def preflight_click(self, target):
        point = super().preflight_click(target)
        if target.element_info.class_name == 'mmui::XOutlineButton':
            self.pending_button = target
            if self.mutate_button_during_preflight:
                target.element_info.name = 'not Send'
        return point

    def _post_click(self, _point):
        button = self.pending_button
        self.clicks.append(button)
        self.click_tree_calls.append(self.tree_calls)
        self.click_generations.append(int(button.element_info.runtime_id.rsplit('-', 1)[1]))
        self.preconditions_at_click.append(self.precondition_count)
        self.submission_path_calls += 1
        text = self.value.CurrentValue
        if self.submit != 'keep_draft':
            self.value.CurrentValue = ''
        if self.submit != 'no_bubble':
            self.bubbles.append(node('new-bubble', text, 'mmui::ChatTextItemView'))
        if self.race_after_click:
            self.chat = 'Bob'
            self.field_node.element_info.name = 'Bob'
            self.session_nodes[0].iface_selection_item.CurrentIsSelected = False

    def nodes(self):
        self.tree_calls += 1
        if self.race_armed:
            self.race_armed = False
            self.chat = 'Bob'
            self.field_node.element_info.name = 'Bob'
            self.session_nodes[0].iface_selection_item.CurrentIsSelected = False

        self.field_node.element_info.name = self.chat
        found = [*self.session_nodes]
        if not (self.omit_field_after_write and self.value.writes):
            found.append(self.field_node)
        found.extend(self.bubbles)
        if ((self.duplicate_field_after_write and self.value.writes)
                or (self.duplicate_field_after_click and self.click_generations)):
            found.append(node('second-field', self.chat, 'mmui::ChatInputField', 'chat_input_field', 'Edit'))
        if self.send_button:
            button = node(
                f'send-generation-{self.tree_calls}', 'Send',
                'mmui::XOutlineButton', control_type='Button',
            )
            button.rectangle = self.send_button.rectangle
            button.iface_invoke = None
            found.append(button)
        return found


class SendSnapshotTests(unittest.TestCase):
    def test_already_selected_send_uses_two_trees_and_clicks_fresh_button(self):
        adapter = SnapshotAdapter()

        result = adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(adapter.click_tree_calls, [2])
        self.assertEqual(adapter.click_generations, [2])
        self.assertEqual(adapter.preconditions_at_click, [2])
        self.assertEqual(adapter.tree_calls - adapter.click_tree_calls[0], 1)

    def test_post_click_race_returns_unknown_without_second_click(self):
        adapter = SnapshotAdapter(race_after_click=True)

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'outcome_unknown')
        self.assertEqual(adapter.click_generations, [2])
        self.assertTrue(adapter.submission_started)
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertEqual(adapter.value.CurrentValue, '')

    def test_post_click_duplicate_field_returns_unknown(self):
        adapter = SnapshotAdapter(duplicate_field_after_click=True)

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'outcome_unknown')
        self.assertEqual(adapter.click_generations, [2])
        self.assertEqual(adapter.value.CurrentValue, '')

    def test_missing_new_bubble_stays_unknown_and_uses_one_tree_per_poll(self):
        adapter = SnapshotAdapter(submit='no_bubble')

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'outcome_unknown')
        self.assertEqual(adapter.click_generations, [2])
        self.assertEqual(adapter.tree_calls - adapter.click_tree_calls[0], 8)
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertEqual(adapter.value.CurrentValue, '')

    def test_new_bubble_with_nonempty_draft_does_not_report_success(self):
        adapter = SnapshotAdapter(submit='keep_draft')

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'outcome_unknown')
        self.assertEqual(adapter.click_generations, [2])
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertEqual(adapter.value.CurrentValue, 'hello')

    def test_selection_race_after_staging_fails_closed_without_click_or_cleanup(self):
        adapter = SnapshotAdapter(race_after_write=True)

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'draft_write_unverified')
        self.assertEqual(adapter.click_generations, [])
        self.assertFalse(adapter.submission_started)
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertEqual(adapter.value.CurrentValue, 'hello')

    def test_duplicate_field_in_final_snapshot_blocks_click(self):
        adapter = SnapshotAdapter(duplicate_field_after_write=True)

        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(adapter.click_generations, [])
        self.assertFalse(adapter.submission_started)
        self.assertEqual(adapter.value.CurrentValue, 'hello')

    def test_missing_field_in_final_snapshot_fails_closed(self):
        adapter = SnapshotAdapter(omit_field_after_write=True)

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'not_found')
        self.assertEqual(adapter.click_generations, [])
        self.assertFalse(adapter.submission_started)
        self.assertEqual(adapter.value.writes, ['hello'])
        self.assertEqual(adapter.value.CurrentValue, 'hello')

    def test_cleanup_does_not_clear_replacement_composer_same_text(self):
        adapter = SnapshotAdapter(replace_field_after_write=True)

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'not_found')
        self.assertFalse(adapter.submission_started)
        self.assertEqual(adapter.original_field.iface_value.CurrentValue, 'hello')
        self.assertEqual(adapter.replacement_value.CurrentValue, 'hello')
        self.assertEqual(adapter.replacement_value.writes, [])

    def test_cleanup_preserves_draft_when_original_runtime_id_is_unavailable(self):
        adapter = SnapshotAdapter(replace_field_after_write=True)
        adapter.field_node.element_info.runtime_id = None

        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(adapter.replacement_value.CurrentValue, 'hello')
        self.assertEqual(adapter.replacement_value.writes, [])

    def test_cleanup_rejects_replacement_with_reused_runtime_id(self):
        adapter = SnapshotAdapter(
            replace_field_after_write=True,
            reuse_field_runtime_id_on_replace=True,
        )

        with self.assertRaises(AdapterError):
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(adapter.replacement_value.CurrentValue, 'hello')
        self.assertEqual(adapter.replacement_value.writes, [])

    def test_button_identity_change_during_preflight_blocks_click(self):
        adapter = SnapshotAdapter(mutate_button_during_preflight=True)

        with self.assertRaises(AdapterError) as caught:
            adapter.send_text(adapter.session_ref(), 'hello')

        self.assertEqual(caught.exception.code, 'send_button_unverified')
        self.assertEqual(adapter.click_generations, [])
        self.assertFalse(adapter.submission_started)
        self.assertEqual(adapter.value.writes, ['hello', ''])
        self.assertEqual(adapter.value.CurrentValue, '')


if __name__ == '__main__':
    unittest.main()
