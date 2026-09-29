"""Owned UIA fake; scrolling never targets a desktop window in these tests."""
import unittest
import time
from unittest.mock import patch

from wxbg import contact_actions
from wxbg.policy import AdapterError
from test_contact_actions import FakeAdapter, Node


class ScrollAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.offset = 0
        self.wheels = []
        self.fail_down = False
        self.fail_up = False
        self.change_content = False
        self.unknown_origin = False

    def nodes(self):
        if self.mode == 'contacts':
            top = 201 if self.unknown_origin else 200
            self.extra_contacts = [] if self.offset else [Node('mmui::ContactsCellMangerBtnView', 'Owned manager',
                'manager', bounds=(150, top, 667, top + 130), parent=self.table)]
            if self.offset:
                self.rows = [self.row('Same label', 'scroll-1', 125), self.row('Same label', 'scroll-2', 1905),
                             self.row('New contact', 'scroll-3', 2010)]
            else:
                self.rows = [self.row('Changed' if self.change_content else 'Alpha', 'row-1', 870),
                             self.row('Beta', 'row-2', 1005), self.row('Alpha', 'row-3', 1140)]
        return super().nodes()

    def wheel(self, adapter, table, delta):
        self.wheels.append(delta)
        self.offset = max(0, self.offset + (1 if delta < 0 else -1))
        if delta < 0 and self.fail_down:
            raise AdapterError('wheel_result_unknown')
        if delta > 0 and self.fail_up:
            raise AdapterError('wheel_result_unknown')


class ContactScrollTests(unittest.TestCase):
    def setUp(self):
        from wxbg import contact_scroll
        self.mod = contact_scroll
        self.adapter = ScrollAdapter()
        for target, value in ((self.mod.time, None),):
            p = patch.object(target, 'sleep', return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(self.mod, '_wheel', side_effect=self.adapter.wheel)
        p.start(); self.addCleanup(p.stop)

    def run_action(self, steps=1, **kwargs):
        return contact_actions.list_contacts(self.adapter, scroll_steps=steps, **kwargs)

    def test_new_root_rejects_old_contacts_table_that_extends_beyond_window(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.mode = 'contacts'
        nodes = self.adapter.nodes()
        guard = contact_actions._Guard(self.adapter, time.monotonic() + 5.0, self.adapter.identity)
        with self.assertRaises(AdapterError) as caught:
            self.mod._snapshot(guard, contact_actions.runtime_id(self.adapter.table), nodes)
        self.assertEqual(caught.exception.code, 'contact_outside_bounds')
        self.assertEqual(self.adapter.wheels, [])

    def test_current_measured_contacts_table_and_manager_row_mark_top(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        self.adapter.mode = 'contacts'
        nodes = self.adapter.nodes()
        for node in nodes:
            if node.element_info.class_name.startswith('mmui::ContactsCell'):
                left, top, right, bottom = node.bounds
                node.bounds = (152, top, 669, bottom)
        guard = contact_actions._Guard(self.adapter, time.monotonic() + 5.0, self.adapter.identity)
        snapshot = self.mod._snapshot(guard, contact_actions.runtime_id(self.adapter.table), nodes)
        self.assertTrue(snapshot['top'])
        self.assertEqual(len(snapshot['contacts']), 3)

    def test_current_measured_layout_scrolls_and_restores_both_views(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        original_nodes = self.adapter.nodes

        def current_nodes():
            nodes = original_nodes()
            if self.adapter.mode == 'contacts':
                for node in nodes:
                    if node.element_info.class_name.startswith('mmui::ContactsCell'):
                        _, top, _, bottom = node.bounds
                        if top >= 1985:
                            bottom, top = 1900 + bottom - top, 1900
                        node.bounds = (152, top, 669, bottom)
            return nodes

        self.adapter.nodes = current_nodes
        result = self.run_action(steps=1)
        self.assertTrue(result['original_contacts_view_restored'])
        self.assertTrue(result['original_conversation_restored'])
        self.assertEqual(self.adapter.wheels, [-120, 120])
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])

    def test_read_scrolled_clipped_rows_keeps_same_names_and_restores_both_views(self):
        result = self.run_action(steps=2)
        self.assertEqual([r['display_text'] for r in result['contacts']], ['Same label', 'Same label', 'New contact'])
        self.assertEqual(len({r['contact_ref'] for r in result['contacts']}), 3)
        self.assertEqual(self.adapter.wheels, [-120, -120, 120, 120])
        self.assertEqual(self.adapter.offset, 0)
        self.assertEqual(self.adapter.mode, 'chat')
        self.assertTrue(result['original_contacts_view_restored'])
        self.assertTrue(result['original_conversation_restored'])
        self.assertTrue(result['not_full_directory'])
        self.assertFalse(result['pagination_supported'])
        self.assertEqual(result['scroll_steps'], 2)
        self.assertTrue(self.adapter.navigation_evidence['scroll']['restored'])

    def test_invalid_steps_reject_before_any_navigation(self):
        for value in (-1, 25, True, 1.0, '1'):
            with self.subTest(value=value), self.assertRaises(AdapterError) as caught:
                self.run_action(value)
            self.assertEqual(caught.exception.code, 'invalid_scroll_steps')
            self.assertEqual(self.adapter.clicks, [])
            self.assertEqual(self.adapter.wheels, [])

    def test_zero_keeps_existing_current_view_path(self):
        self.adapter.unknown_origin = True
        result = self.run_action(0)
        self.assertEqual(self.adapter.wheels, [])
        self.assertEqual(result['scroll_steps'], 0)

    def test_non_top_origin_does_not_scroll_but_returns_chat(self):
        self.adapter.unknown_origin = True
        with self.assertRaises(AdapterError) as caught:
            self.run_action()
        self.assertEqual(caught.exception.code, 'contacts_top_required')
        self.assertEqual(self.adapter.wheels, [])
        self.assertEqual(self.adapter.mode, 'chat')

    def test_unknown_down_result_is_not_retried_and_gets_inverse_cleanup(self):
        self.adapter.fail_down = True
        with self.assertRaises(AdapterError) as caught:
            self.run_action(3)
        self.assertEqual(caught.exception.code, 'wheel_result_unknown')
        self.assertEqual(self.adapter.wheels, [-120, 120])
        self.assertTrue(self.adapter.navigation_evidence['scroll']['restored'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_failed_inverse_is_error_even_when_chat_restored(self):
        self.adapter.fail_up = True
        with self.assertRaises(AdapterError) as caught:
            self.run_action()
        self.assertEqual(caught.exception.code, 'contacts_scroll_restore_failed')
        self.assertFalse(self.adapter.navigation_evidence['scroll']['restored'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_changed_origin_content_fails_restoration_without_leaking_name(self):
        original = self.adapter.wheel
        def change(adapter, table, delta):
            original(adapter, table, delta)
            if delta > 0: self.adapter.change_content = True
        with patch.object(self.mod, '_wheel', side_effect=change), self.assertRaises(AdapterError) as caught:
            self.run_action()
        self.assertEqual(caught.exception.code, 'contacts_scroll_restore_failed')
        self.assertNotIn('Changed', str(caught.exception))
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_limit_and_query_apply_to_destination_only(self):
        result = self.run_action(1, query='same', limit=1)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['exposed_count'], 3)
        self.assertEqual(result['query_scope'], 'requested_contact_view_only')

    def test_scrolled_rows_must_have_observed_height_and_ownership(self):
        original = self.adapter.nodes
        def invalid():
            nodes = original()
            if self.adapter.mode == 'contacts' and self.adapter.offset:
                self.adapter.rows[0].bounds = (150, 100, 667, 300)
            return nodes
        with patch.object(self.adapter, 'nodes', side_effect=invalid), self.assertRaises(AdapterError) as caught:
            self.run_action()
        self.assertEqual(caught.exception.code, 'contact_outside_bounds')
        self.assertTrue(self.adapter.navigation_evidence['scroll']['restored'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_maximum_steps_is_bounded(self):
        self.run_action(24)
        self.assertEqual(self.adapter.wheels, [-120] * 24 + [120] * 24)

    def test_scroll_timeout_keeps_budget_for_original_chat_cleanup(self):
        now = [0.0]
        old = self.adapter.wheel
        def slow(adapter, table, delta):
            old(adapter, table, delta)
            now[0] += .6
        with patch.object(self.mod.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(self.mod, '_wheel', side_effect=slow), self.assertRaises(AdapterError) as caught:
            self.run_action(24)
        self.assertEqual(caught.exception.code, 'contacts_scroll_restore_failed')
        self.assertTrue(self.adapter.navigation_evidence['restored'])
        self.assertFalse(self.adapter.navigation_evidence['scroll']['restored'])
        self.assertLess(now[0], 24)

    def test_no_effect_does_not_claim_end_of_directory(self):
        with patch.object(self.mod, '_wheel', side_effect=lambda *args: None):
            result = self.run_action(2)
        self.assertFalse(result['viewport_changed'])
        self.assertTrue(result['not_full_directory'])
        self.assertNotIn('end_of_directory', result)
        self.assertTrue(result['original_contacts_view_restored'])

    def test_unstable_destination_runtime_ids_fail_with_successful_cleanup(self):
        old = self.adapter.nodes
        generation = [0]
        def changing():
            nodes = old()
            if self.adapter.mode == 'contacts' and self.adapter.offset:
                generation[0] += 1
                self.adapter.rows[0].info.runtime_id = f'volatile-{generation[0]}'
            return nodes
        with patch.object(self.adapter, 'nodes', side_effect=changing), self.assertRaises(AdapterError) as caught:
            self.run_action()
        self.assertEqual(caught.exception.code, 'contacts_view_not_settled')
        self.assertTrue(self.adapter.navigation_evidence['scroll']['restored'])

    def test_restoration_allows_stable_recreated_nodes_with_same_view_content(self):
        old = self.adapter.nodes
        def recreated():
            nodes = old()
            if self.adapter.mode == 'contacts' and 120 in self.adapter.wheels:
                for row in self.adapter.rows: row.info.runtime_id += '-recreated'
            return nodes
        with patch.object(self.adapter, 'nodes', side_effect=recreated):
            result = self.run_action()
        self.assertTrue(result['original_contacts_view_restored'])

    def test_unstable_restoration_nodes_cannot_claim_scroll_restored(self):
        old = self.adapter.nodes
        generation = [0]
        def changing():
            nodes = old()
            if self.adapter.mode == 'contacts' and 120 in self.adapter.wheels:
                generation[0] += 1
                self.adapter.rows[0].info.runtime_id = f'volatile-{generation[0]}'
            return nodes
        with patch.object(self.adapter, 'nodes', side_effect=changing), self.assertRaises(AdapterError) as caught:
            self.run_action()
        self.assertEqual(caught.exception.code, 'contacts_scroll_restore_failed')
        self.assertTrue(self.adapter.navigation_evidence['restored'])
        self.assertFalse(self.adapter.navigation_evidence['scroll']['restored'])

    def test_primary_scroll_and_chat_cleanup_failures_remain_distinct(self):
        self.adapter.fail_down = self.adapter.fail_up = True
        self.adapter.on_click = lambda node: setattr(self.adapter, 'mode', 'contacts')
        with self.assertRaises(AdapterError) as caught: self.run_action()
        self.assertEqual(caught.exception.code, 'navigation_restore_failed')
        nav = self.adapter.navigation_evidence
        self.assertEqual(nav['primary_error_code'], 'contacts_scroll_restore_failed')
        self.assertEqual(nav['cleanup_error_code'], 'original_chat_not_restored')
        self.assertEqual(nav['scroll']['primary_error_code'], 'wheel_result_unknown')
        self.assertEqual(nav['scroll']['cleanup_error_code'], 'wheel_result_unknown')


if __name__ == '__main__': unittest.main()
