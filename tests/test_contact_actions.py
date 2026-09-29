"""Owned fake UIA only: never import the Windows adapter or operate Weixin."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg.policy import AdapterError, stable_ref

_local = Path(__file__).with_name('contact_actions.py')
if _local.exists():
    _spec = importlib.util.spec_from_file_location('owned_contact_actions', _local)
    contact_actions = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(contact_actions)
else:
    try:
        from wxbg import contact_actions
    except ImportError:
        contact_actions = None


class Node:
    def __init__(self, kind, name, rid, control='Group', aid='', bounds=(0, 0, 1, 1), parent=None):
        self.info = SimpleNamespace(class_name=kind, name=name, runtime_id=rid,
                                    control_type=control, automation_id=aid)
        self.bounds = bounds
        self.parent_node = parent
        self.stale = False
        self.on_rectangle = None
        self.on_parent = None
        self.selected = False
        self.selection_error = False

    @property
    def element_info(self):
        if self.stale:
            raise RuntimeError('owned stale node with private label')
        return self.info

    @property
    def iface_selection_item(self):
        return self

    @property
    def CurrentIsSelected(self):
        if self.selection_error:
            raise RuntimeError('owned selection provider failed')
        return self.selected

    def rectangle(self):
        if self.stale:
            raise RuntimeError('owned stale rectangle')
        if self.on_rectangle:
            self.on_rectangle()
        return SimpleNamespace(**dict(zip(('left', 'top', 'right', 'bottom'), self.bounds)))

    def parent(self):
        if self.stale:
            raise RuntimeError('owned stale parent')
        if self.on_parent:
            self.on_parent()
        return self.parent_node


class Draft:
    def __init__(self, adapter):
        self.adapter = adapter

    @property
    def CurrentValue(self):
        if self.adapter.on_draft:
            self.adapter.on_draft()
        if self.adapter.draft_error:
            raise RuntimeError('owned draft provider failed')
        return self.adapter.text

    def SetValue(self, text):
        self.adapter.writes.append(text)
        raise AssertionError('No draft writes are permitted')


class FakeAdapter:
    def __init__(self):
        self.identity = 'owned-pid:owned-created'
        self.root_node = Node('Root', '', 'root', bounds=(0, 0, 3240, 2040))
        self.wechat = Node('mmui::XTabBarItem', '微信', 'wx-tab', 'Button',
                           bounds=(0, 240, 150, 330), parent=self.root_node)
        self.contacts = Node('mmui::XTabBarItem', '通訊錄', 'contacts-tab', 'Button',
                             bounds=(0, 360, 150, 450), parent=self.root_node)
        self.session = Node('mmui::ChatSessionCell', 'Owned original preview', 'session-1',
                            'ListItem', aid='session_item_Owned original', parent=self.root_node)
        self.session.selected = True
        self.field = Node('mmui::ChatInputField', 'Owned original', 'field-1',
                          'Edit', aid='chat_input_field', parent=self.root_node)
        self.field.iface_value = Draft(self)
        self.table = Node('mmui::ContactsTableBaseView', '', 'table-1',
                          bounds=(150, 200, 667, 2030), parent=self.root_node)
        self.group = Node('QWidget', '', 'rows-parent', parent=self.table)
        self.rows = [self.row('Alpha', 'row-1', 870), self.row('Beta', 'row-2', 1005),
                     self.row('Alpha', 'row-3', 1140)]
        self.extra_chat = []
        self.extra_contacts = []
        self.mode = 'chat'
        self.text = ''
        self.clicks = []
        self.writes = []
        self.on_click = None
        self.on_nodes = None
        self.on_draft = None
        self.draft_error = False
        self.block_code = None
        self.nodes_calls = 0
        self.precondition_calls = 0
        self.navigation_evidence = None

    def row(self, name, rid, top):
        return Node('mmui::ContactsCellItemView', name, rid, 'ListItem',
                    bounds=(150, top, 667, top + 135), parent=self.group)

    def precondition(self):
        self.precondition_calls += 1
        if self.block_code:
            raise AdapterError(self.block_code)

    def root(self):
        return self.root_node

    def nodes(self):
        self.nodes_calls += 1
        if self.on_nodes:
            self.on_nodes()
        tabs = [self.wechat, self.contacts]
        if self.mode == 'chat':
            return tabs + [self.session, self.field] + self.extra_chat
        return tabs + [self.table, self.group] + self.rows + self.extra_contacts

    def ref(self, node, context=''):
        i = node.element_info
        name = i.automation_id if i.class_name == 'mmui::ChatSessionCell' else i.automation_id + '|' + i.name
        return stable_ref(self.identity, context, str(i.runtime_id), i.class_name, name)

    def click(self, node):
        self.precondition()
        if node not in (self.wechat, self.contacts):
            raise AssertionError('Only the two sidebar tabs may be clicked')
        self.clicks.append(node.info.name)
        if self.on_click:
            self.on_click(node)
        else:
            self.mode = 'contacts' if node is self.contacts else 'chat'


class ContactActionsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(contact_actions, 'The bounded contacts orchestration is not implemented')
        self.adapter = FakeAdapter()
        self.sleep = patch.object(contact_actions.time, 'sleep', return_value=None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def run_action(self, **args):
        return contact_actions.list_contacts(self.adapter, **args)

    def failure(self, code=None, **args):
        with self.assertRaises(AdapterError) as raised:
            self.run_action(**args)
        if code:
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.adapter.writes, [])
        return raised.exception

    def test_verified_current_root_geometry_passes_read_only_guard(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        guard = contact_actions._Guard(
            self.adapter, contact_actions.time.monotonic() + 5.0, self.adapter.identity)
        contact_actions._root(guard)
        self.assertEqual(self.adapter.clicks, [])

    def test_new_window_size_uses_live_sidebar_and_table_geometry(self):
        self.adapter.root_node.bounds = (300, 80, 1900, 1080)
        self.adapter.wechat.bounds = (300, 240, 380, 290)
        self.adapter.contacts.bounds = (300, 310, 380, 360)
        self.adapter.table.bounds = (380, 180, 760, 1000)
        for row, top in zip(self.adapter.rows, (350, 485, 620)):
            row.bounds = (380, top, 760, top + 135)
        result = self.run_action()
        self.assertEqual(result['count'], 3)
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(result['original_conversation_restored'])

    def test_current_root_rejects_retained_old_contact_table_outside_bounds(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.mode = 'contacts'
        guard = contact_actions._Guard(
            self.adapter, contact_actions.time.monotonic() + 5.0, self.adapter.identity)
        with self.assertRaises(AdapterError) as caught:
            contact_actions._read_rows(guard, self.adapter.nodes(), self.adapter.table)
        self.assertEqual(caught.exception.code, 'contact_outside_bounds')
        self.assertEqual(self.adapter.clicks, [])

    def test_old_root_rejects_current_contacts_table_even_when_contained(self):
        self.adapter.table.bounds = (152, 200, 669, 1985)
        self.adapter.mode = 'contacts'
        for row in self.adapter.rows:
            _, top, _, bottom = row.bounds
            row.bounds = (152, top, 669, bottom)
        guard = contact_actions._Guard(
            self.adapter, contact_actions.time.monotonic() + 5.0, self.adapter.identity)
        with self.assertRaises(AdapterError) as caught:
            contact_actions._read_rows(guard, self.adapter.nodes(), self.adapter.table)
        self.assertEqual(caught.exception.code, 'contact_outside_bounds')

    def test_current_measured_contacts_table_can_be_read_and_chat_restored(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        for row in self.adapter.rows:
            _, top, _, bottom = row.bounds
            row.bounds = (152, top, 669, bottom)
        result = self.run_action()
        self.assertEqual(result['count'], 3)
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(result['original_conversation_restored'])

    def test_current_virtualized_bottom_row_is_read_without_row_activation(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        for row in self.adapter.rows:
            _, top, _, bottom = row.bounds
            row.bounds = (152, top, 669, bottom)
        self.adapter.rows.append(self.adapter.row('Clipped', 'row-4', 1905))
        self.adapter.rows[-1].bounds = (152, 1905, 669, 2040)
        result = self.run_action()
        self.assertEqual(result['count'], 4)
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(result['original_conversation_restored'])

    def test_current_virtualized_tail_larger_than_measured_is_rejected(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        self.adapter.mode = 'contacts'
        row = self.adapter.row('Clipped', 'row-4', 1906)
        row.bounds = (152, 1906, 669, 2041)
        self.adapter.rows = [row]
        guard = contact_actions._Guard(
            self.adapter, contact_actions.time.monotonic() + 5.0, self.adapter.identity)
        with self.assertRaises(AdapterError) as caught:
            contact_actions._read_rows(guard, self.adapter.nodes(), self.adapter.table)
        self.assertEqual(caught.exception.code, 'contact_outside_bounds')

    def test_current_virtualized_tail_requires_exact_table_width(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        self.adapter.mode = 'contacts'
        row = self.adapter.row('Clipped', 'row-4', 1905)
        row.bounds = (151, 1905, 669, 2040)
        self.adapter.rows = [row]
        guard = contact_actions._Guard(
            self.adapter, contact_actions.time.monotonic() + 5.0, self.adapter.identity)
        with self.assertRaises(AdapterError) as caught:
            contact_actions._read_rows(guard, self.adapter.nodes(), self.adapter.table)
        self.assertEqual(caught.exception.code, 'contact_outside_bounds')

    def test_current_measured_sidebar_tabs_are_used_for_entry_and_restore(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        # The Contacts table is measured separately; isolate the two tab gates.
        with patch.object(contact_actions, '_read_rows', return_value=[]):
            result = self.run_action()
        self.assertEqual(result['count'], 0)
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])

    def test_success_preserves_duplicate_labels_and_temporary_refs(self):
        result = self.run_action()
        self.assertEqual([x['display_text'] for x in result['contacts']], ['Alpha', 'Beta', 'Alpha'])
        refs = [x['contact_ref'] for x in result['contacts']]
        self.assertEqual(len(set(refs)), 3)
        self.assertEqual(refs, [self.adapter.ref(x, 'contacts') for x in self.adapter.rows])
        self.assertEqual((result['count'], result['exposed_count']), (3, 3))
        self.assertTrue(result['visible_only'])
        self.assertTrue(result['not_full_directory'])
        self.assertFalse(result['pagination_supported'])
        self.assertIs(result.get('original_conversation_restored'), True)
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertEqual((self.adapter.mode, self.adapter.text), ('chat', ''))
        self.assertTrue(self.adapter.navigation_evidence['restored'])
        self.assertIsNone(self.adapter.navigation_evidence['primary_error_code'])
        self.assertLessEqual(self.adapter.nodes_calls, 8)

    def test_query_and_limit_only_filter_exposed_labels(self):
        result = self.run_action(query='aLP', limit=1)
        self.assertEqual([x['display_text'] for x in result['contacts']], ['Alpha'])
        self.assertEqual((result['query'], result['limit'], result['count'], result['exposed_count']), ('aLP', 1, 1, 3))
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])

    def test_background_checks_are_phase_bounded_not_per_contact_or_ancestor(self):
        self.run_action()
        small_count = self.adapter.precondition_calls
        self.adapter = FakeAdapter()
        self.adapter.rows = [self.adapter.row('Owned ' + str(i), 'row-' + str(i), 870) for i in range(30)]
        self.run_action()
        self.assertEqual(self.adapter.precondition_calls, small_count)
        self.assertLessEqual(small_count, 36)

    def test_no_match_is_successfully_restored_empty_result(self):
        result = self.run_action(query='not an exposed name')
        self.assertEqual((result['contacts'], result['count'], result['exposed_count']), ([], 0, 3))
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_invalid_query_or_limit_never_reads_or_clicks(self):
        for args in ({'query': None}, {'query': 1}, {'query': 'x' * 257}, {'query': 'x\0y'},
                     {'limit': True}, {'limit': 1.0}, {'limit': 0}, {'limit': 101}, {'limit': '1'}):
            with self.subTest(args=args):
                self.adapter = FakeAdapter()
                self.failure(**args)
                self.assertEqual((self.adapter.nodes_calls, self.adapter.clicks), (0, []))

    def test_existing_draft_never_navigates(self):
        self.adapter.text = 'private draft stays untouched'
        self.failure('draft_conflict')
        self.assertEqual(self.adapter.clicks, [])
        self.assertEqual(self.adapter.text, 'private draft stays untouched')

    def test_wrong_header_never_navigates(self):
        self.adapter.field.info.name = 'Another conversation'
        self.failure('context_conflict')
        self.assertEqual(self.adapter.clicks, [])

    def test_mixed_chat_and_contacts_page_is_not_accepted_as_original_chat(self):
        self.adapter.extra_chat.append(self.adapter.table)
        self.failure('context_conflict')
        self.assertEqual(self.adapter.clicks, [])

    def test_mixed_page_after_restore_is_not_reported_restored(self):
        def leave_contact_table(node):
            self.adapter.mode = 'contacts' if node is self.adapter.contacts else 'chat'
            if node is self.adapter.wechat:
                self.adapter.extra_chat.append(self.adapter.table)
        self.adapter.on_click = leave_contact_table
        self.failure('navigation_restore_failed')
        self.assertFalse(self.adapter.navigation_evidence['restored'])

    def test_no_selected_session_never_navigates(self):
        self.adapter.session.selected = False
        self.failure('context_conflict')
        self.assertEqual(self.adapter.clicks, [])

    def test_multiple_selected_sessions_never_navigate(self):
        other = Node('mmui::ChatSessionCell', 'Other', 'session-2', 'ListItem', 'session_item_Other')
        other.selected = True
        self.adapter.extra_chat.append(other)
        self.failure('context_conflict')
        self.assertEqual(self.adapter.clicks, [])

    def test_duplicate_original_title_never_navigates(self):
        self.adapter.extra_chat.append(Node('mmui::ChatSessionCell', 'Duplicate', 'session-2',
                                             'ListItem', 'session_item_Owned original'))
        self.failure('ambiguous_recipient')
        self.assertEqual(self.adapter.clicks, [])

    def test_bad_selection_provider_is_not_treated_as_unselected(self):
        self.adapter.session.selection_error = True
        self.failure('observation_failed')
        self.assertEqual(self.adapter.clicks, [])

    def test_invalid_selection_value_rejected(self):
        self.adapter.session.selected = 'true'
        self.failure('observation_failed')
        self.assertEqual(self.adapter.clicks, [])

    def test_shifted_root_or_tab_geometry_never_clicks(self):
        for target in ('root_node', 'wechat', 'contacts'):
            with self.subTest(target=target):
                self.adapter = FakeAdapter()
                getattr(self.adapter, target).bounds = (1, 2, 3, 4)
                self.failure('unverified_layout')
                self.assertEqual(self.adapter.clicks, [])

    def test_draft_changed_during_geometry_work_is_rechecked_before_click(self):
        self.adapter.contacts.on_rectangle = lambda: setattr(self.adapter, 'text', 'new private draft')
        self.failure('draft_conflict')
        self.assertEqual(self.adapter.clicks, [])

    def test_selected_ref_changed_during_geometry_never_clicks(self):
        self.adapter.contacts.on_rectangle = lambda: setattr(self.adapter.session.info, 'runtime_id', 'replacement')
        self.failure('context_conflict')
        self.assertEqual(self.adapter.clicks, [])

    def test_noop_contacts_click_has_bounded_poll_and_no_extra_restore_click(self):
        self.adapter.on_click = lambda node: None
        self.failure('contacts_not_opened')
        self.assertEqual(self.adapter.clicks, ['通訊錄'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])
        self.assertLessEqual(self.adapter.nodes_calls, 12)

    def test_contact_read_provider_error_still_restores(self):
        def parent_failure():
            raise RuntimeError('owned private provider error')
        self.adapter.rows[0].on_parent = parent_failure
        self.failure('observation_failed')
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_stale_contact_info_still_restores_using_valid_known_tab(self):
        self.adapter.rows[0].stale = True
        self.failure('observation_failed')
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_contacts_enumeration_failure_still_restores(self):
        def failed_nodes():
            if self.adapter.mode == 'contacts':
                raise RuntimeError('owned enumeration failed')
        self.adapter.on_nodes = failed_nodes
        self.failure('observation_failed')
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_contact_outside_table_ancestry_rejected(self):
        self.adapter.rows[0].parent_node = self.adapter.root_node
        self.failure('contact_outside_table')
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_parent_cycle_is_bounded_and_rejected(self):
        self.adapter.group.parent_node = self.adapter.group
        self.failure('contact_outside_table')
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_contact_outside_root_or_without_table_intersection_rejected(self):
        for bounds in ((150, 1950, 667, 2100), (700, 800, 900, 950), (0, 0, 0, 0)):
            with self.subTest(bounds=bounds):
                self.adapter = FakeAdapter()
                self.adapter.rows[0].bounds = bounds
                self.failure('contact_outside_bounds')
                self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_observed_bottom_row_clipping_is_allowed_with_real_ancestry(self):
        self.adapter.rows[-1].bounds = (150, 1905, 667, 2040)
        self.assertEqual(self.run_action()['count'], 3)

    def test_ambiguous_table_rejected_and_restored(self):
        self.adapter.extra_contacts.append(Node('mmui::ContactsTableBaseView', '', 'table-2'))
        self.failure('ambiguous_contacts_table')
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_changed_page_at_contact_read_boundary_rejected(self):
        self.adapter.rows[-1].on_parent = lambda: setattr(self.adapter, 'mode', 'chat')
        self.failure('contacts_page_changed')
        self.assertEqual(self.adapter.clicks, ['通訊錄'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_changed_label_during_geometry_is_not_returned_under_old_ref(self):
        self.adapter.rows[0].on_rectangle = lambda: setattr(self.adapter.rows[0].info, 'name', 'Changed')
        self.failure('contact_changed')
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_restore_wrong_original_ref_fails_without_clicking_any_chat(self):
        def change_ref(node):
            self.adapter.mode = 'contacts' if node is self.adapter.contacts else 'chat'
            if node is self.adapter.wechat:
                self.adapter.session.info.runtime_id = 'replacement-session'
        self.adapter.on_click = change_ref
        self.failure('navigation_restore_failed')
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertEqual(self.adapter.navigation_evidence['cleanup_error_code'], 'context_conflict')
        self.assertFalse(self.adapter.navigation_evidence['restored'])

    def test_restore_draft_change_is_preserved_and_fails(self):
        def draft_appears(node):
            self.adapter.mode = 'contacts' if node is self.adapter.contacts else 'chat'
            if node is self.adapter.wechat:
                self.adapter.text = 'new draft while reading contacts'
        self.adapter.on_click = draft_appears
        self.failure('navigation_restore_failed')
        self.assertEqual(self.adapter.text, 'new draft while reading contacts')
        self.assertEqual(self.adapter.navigation_evidence['cleanup_error_code'], 'draft_conflict')

    def test_final_geometry_work_cannot_hide_new_draft(self):
        def geometry():
            if self.adapter.clicks == ['通訊錄', '微信']:
                self.adapter.text = 'final-boundary draft'
        self.adapter.root_node.on_rectangle = geometry
        self.failure('navigation_restore_failed')
        self.assertEqual(self.adapter.navigation_evidence['cleanup_error_code'], 'draft_conflict')

    def test_primary_read_error_and_cleanup_error_are_both_preserved_without_private_text(self):
        self.adapter.rows[0].on_parent = lambda: (_ for _ in ()).throw(AdapterError('owned_read_failure', 'private detail'))
        def fail_restore(node):
            if node is self.adapter.contacts:
                self.adapter.mode = 'contacts'
            else:
                raise AdapterError('owned_restore_failure', 'private detail')
        self.adapter.on_click = fail_restore
        self.failure('navigation_restore_failed')
        ev = self.adapter.navigation_evidence
        self.assertEqual((ev['primary_error_code'], ev['cleanup_error_code']), ('owned_read_failure', 'owned_restore_failure'))
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertNotIn('private', repr(ev))
        self.assertFalse(ev['restored'])

    def test_visible_popup_at_cleanup_blocks_the_restore_click(self):
        self.adapter.rows[-1].on_parent = lambda: setattr(self.adapter, 'block_code', 'existing_popup')
        self.failure('navigation_restore_failed')
        self.assertEqual(self.adapter.clicks, ['通訊錄'])
        self.assertEqual(self.adapter.navigation_evidence['cleanup_error_code'], 'existing_popup')

    def test_partial_entry_click_exception_still_attempts_one_restore(self):
        def partial_click(node):
            self.adapter.mode = 'contacts' if node is self.adapter.contacts else 'chat'
            if node is self.adapter.contacts:
                raise AdapterError('owned_transport_failure')
        self.adapter.on_click = partial_click
        self.failure('owned_transport_failure')
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_soft_read_deadline_leaves_time_to_restore(self):
        clock = [0.0]
        self.adapter.rows[0].on_parent = lambda: clock.__setitem__(0, 16.0)
        with patch.object(contact_actions.time, 'monotonic', side_effect=lambda: clock[0]):
            self.failure('contacts_deadline')
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(self.adapter.navigation_evidence['restored'])

    def test_soft_deadline_before_entry_never_clicks(self):
        clock = [0.0]
        self.adapter.contacts.on_rectangle = lambda: clock.__setitem__(0, 16.0)
        with patch.object(contact_actions.time, 'monotonic', side_effect=lambda: clock[0]):
            self.failure('contacts_deadline')
        self.assertEqual(self.adapter.clicks, [])


if __name__ == '__main__':
    unittest.main()
