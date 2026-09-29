"""Pure owned fake tests. No Windows/COM imports or real client operations."""
from pathlib import Path
from types import SimpleNamespace
import unittest

from wxbg.policy import AdapterError, stable_ref

try:
    from wxbg import history_view as module
except ModuleNotFoundError:
    module = None


class Node:
    def __init__(self, kind, runtime, *, name='', control='Group', aid='',
                 rect=(0, 0, 1, 1), parent=None, handle=0):
        self.info = SimpleNamespace(class_name=kind, runtime_id=runtime, name=name,
                                    control_type=control, automation_id=aid)
        self.bounds = rect; self.parent_node = parent; self.handle = handle
        self.selected = False; self.text = ''; self.stale = False
        self.on_parent = self.on_rect = self.on_value = None

    @property
    def element_info(self):
        if self.stale: raise RuntimeError('PRIVATE stale provider text')
        return self.info

    @property
    def iface_selection_item(self): return self

    @property
    def CurrentIsSelected(self): return self.selected

    @property
    def iface_value(self): return self

    @property
    def CurrentValue(self):
        if self.on_value: self.on_value()
        return self.text

    def SetValue(self, value): raise AssertionError('draft write forbidden')

    def rectangle(self):
        if self.on_rect: self.on_rect()
        if self.stale: raise RuntimeError('PRIVATE rectangle failure')
        return SimpleNamespace(**dict(zip(('left', 'top', 'right', 'bottom'), self.bounds)))

    def parent(self):
        if self.on_parent: self.on_parent()
        if self.stale: raise RuntimeError('PRIVATE parent failure')
        return self.parent_node


class Adapter:
    def __init__(self):
        self.hwnd = 123; self.identity = 'owned-process-instance'
        self.root_node = Node('mmui::MainWindow', 'root', control='Window',
                              rect=(0, 0, 3240, 2040), handle=self.hwnd)
        self.session = Node('mmui::ChatSessionCell', 'session', control='ListItem',
                            aid='session_item_Owned', parent=self.root_node)
        self.session.selected = True
        self.field = Node('mmui::ChatInputField', 'field', control='Edit', name='Owned',
                          aid='chat_input_field', parent=self.root_node)
        self.view = Node('mmui::MessageView', 'view', rect=(667, 200, 3229, 1680), parent=self.root_node)
        self.list = Node('mmui::RecyclerListView', 'list', control='List',
                         rect=self.view.bounds, parent=self.view)
        self.rows = [self.row('Same text', 'first', (667, 105, 3229, 245)),
                     self.row('Same text', 'second', (667, 245, 3229, 347)),
                     self.row('Synthetic card', 'third', (667, 347, 3229, 649), kind='mmui::ChatBubbleItemView')]
        self.extra = []; self.walks = 0; self.preconditions = 0; self.on_nodes = None; self.block = None

    def row(self, text, runtime, rect, *, parent=None, kind='mmui::ChatTextItemView'):
        return Node(kind, runtime, name=text, control='ListItem', rect=rect,
                    parent=self.list if parent is None else parent)

    def nodes(self):
        self.walks += 1
        if self.on_nodes: self.on_nodes()
        return [self.session, self.field, self.view, self.list] + self.rows + self.extra

    def root(self): return self.root_node

    def precondition(self):
        self.preconditions += 1
        if self.block: raise AdapterError(self.block)

    def ref(self, node, context=''):
        i = node.element_info
        return stable_ref(self.identity, context, str(i.runtime_id), i.class_name, i.automation_id)

    def click(self, *args): raise AssertionError('input forbidden')


class HistoryViewTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'work-only history observer not implemented')
        self.adapter = Adapter()

    def observe(self, **kwargs): return module.observe_history(self.adapter, **kwargs)

    def use_current_measured_layout(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.view.bounds = (670, 200, 3227, 1635)
        self.adapter.list.bounds = (670, 200, 3227, 1635)
        for row in self.adapter.rows:
            _, top, _, bottom = row.bounds
            row.bounds = (670, top, 3227, bottom)

    def refused(self, code=None, **kwargs):
        with self.assertRaises(AdapterError) as caught: self.observe(**kwargs)
        if code: self.assertEqual(caught.exception.code, code)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_single_walk_clipped_variable_rows_and_duplicates_preserved(self):
        view = self.observe()
        self.assertEqual(self.adapter.walks, 1)
        self.assertEqual(view.context, ('session_item_Owned', 'session', 'field'))
        self.assertEqual(view.title, 'Owned')
        self.assertEqual(view.session_ref, self.adapter.ref(self.adapter.session))
        self.assertIs(view.view_node, self.adapter.view); self.assertIs(view.list_node, self.adapter.list)
        self.assertEqual([row.text for row in view.rows], ['Same text', 'Same text', 'Synthetic card'])
        self.assertEqual(view.rows[0].rect, (667, 105, 3229, 245))
        self.assertEqual(view.signature[0], ('mmui::ChatTextItemView', 'Same text', (667, 105, 3229, 245)))

    def test_current_measured_root_and_history_view_are_accepted_together(self):
        self.use_current_measured_layout()
        view = self.observe()
        self.assertEqual(view.frame_identity, ('view', 'list'))
        self.assertEqual(len(view.rows), 3)

    def test_new_window_size_uses_observed_message_frame(self):
        self.adapter.root_node.bounds = (300, 80, 1900, 1080)
        self.adapter.view.bounds = (680, 200, 1800, 800)
        self.adapter.list.bounds = self.adapter.view.bounds
        for row in self.adapter.rows:
            _, top, _, bottom = row.bounds
            row.bounds = (680, top, 1800, bottom)
        view = self.observe()
        self.assertEqual(view.rows[0].rect, (680, 105, 1800, 245))
        self.assertEqual(view.frame_identity, ('view', 'list'))

    def test_current_measured_direct_image_reference_row_is_accepted(self):
        self.use_current_measured_layout()
        self.adapter.rows[-1].info.class_name = 'mmui::ChatBubbleReferItemView'
        self.adapter.rows[-1].bounds = (670, 1332, 3227, 1634)
        self.adapter.rows[0].bounds = (670, 105, 3227, 245)
        self.adapter.rows[1].bounds = (670, 245, 3227, 1332)
        view = self.observe()
        self.assertEqual(view.rows[-1].kind, 'mmui::ChatBubbleReferItemView')
        self.assertEqual(view.rows[-1].rect, (670, 1332, 3227, 1634))

        self.adapter.rows[-1].info.control_type = 'Button'
        self.refused('history_row_unsupported')

    def test_old_root_with_current_history_view_is_rejected(self):
        self.use_current_measured_layout()
        self.adapter.root_node.bounds = (0, 0, 3240, 2040)
        self.assertEqual(self.observe().frame_identity, ('view', 'list'))

    def test_current_root_with_old_history_view_is_rejected(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.assertEqual(self.observe().frame_identity, ('view', 'list'))

    def test_provider_order_is_not_assumed(self):
        self.adapter.rows.reverse()
        self.assertEqual([r.runtime for r in self.observe().rows], ['first', 'second', 'third'])

    def test_signature_omits_recreated_runtime_ids(self):
        first = self.observe()
        for row in self.adapter.rows: row.info.runtime_id += '-recreated'
        second = self.observe(expected_context=first.context)
        self.assertEqual(first.signature, second.signature)
        self.assertNotEqual(first.rows[0].runtime, second.rows[0].runtime)

    def test_public_phase_identities_are_frame_then_ordered_row_runtimes(self):
        self.adapter.rows.reverse()
        view = self.observe()
        self.assertEqual(getattr(view, 'frame_identity', None), ('view', 'list'))
        self.assertEqual(getattr(view, 'row_identity', None), ('first', 'second', 'third'))

    def test_non_direct_message_descendants_and_other_pane_rows_not_collected(self):
        self.adapter.extra = [self.adapter.row('PRIVATE nested text', 'nested', (667, 400, 3229, 500), parent=self.adapter.rows[-1]),
                              self.adapter.row('PRIVATE elsewhere', 'foreign', (667, 500, 3229, 600), parent=self.adapter.root_node)]
        self.assertEqual(len(self.observe().rows), 3)

    def test_duplicate_row_runtime_rejected(self):
        self.adapter.rows[1].info.runtime_id = 'first'; self.refused('history_row_identity_invalid')

    def test_overlap_rows_rejected(self):
        self.adapter.rows[1].bounds = (667, 200, 3229, 347); self.refused('history_rows_overlap')

    def test_outside_zero_area_wrong_horizontal_rejected(self):
        for rect in ((667, 100, 3229, 200), (667, 1700, 3229, 1800),
                     (667, 200, 3229, 200), (666, 200, 3229, 245)):
            with self.subTest(rect=rect):
                self.adapter.rows[0].bounds = rect; self.refused('history_row_geometry_invalid')

    def test_unsupported_direct_message_class_and_type_rejected(self):
        self.adapter.rows[0].info.class_name = 'mmui::ChatUnknownItemView'
        self.refused('history_row_unsupported')
        self.adapter.rows[0].info.class_name = 'mmui::ChatTextItemView'
        self.adapter.rows[0].info.control_type = 'Button'; self.refused('history_row_unsupported')

    def test_nonempty_draft_never_cleared(self):
        self.adapter.field.text = 'PRIVATE foreign draft'; self.refused('draft_conflict')
        self.assertEqual(self.adapter.field.text, 'PRIVATE foreign draft')

    def test_context_change_from_value_read_is_detected(self):
        self.adapter.field.on_value = lambda: setattr(self.adapter.session, 'selected', False)
        self.refused('context_conflict')

    def test_ambiguous_session_title_and_multiple_selection_rejected(self):
        duplicate = Node('mmui::ChatSessionCell', 'other', aid='session_item_Owned', control='ListItem')
        self.adapter.extra = [duplicate]; self.refused('ambiguous_recipient')
        duplicate.info.automation_id = 'session_item_Other'; duplicate.selected = True
        self.refused('context_conflict')

    def test_header_must_match_session(self):
        self.adapter.field.info.name = 'Other'; self.refused('context_conflict')

    def test_expected_context_pins_field_and_session(self):
        prior = self.observe()
        self.adapter.field.info.runtime_id = 'replacement'; self.refused('context_conflict', expected_context=prior.context)
        self.adapter.field.info.runtime_id = 'field'; self.adapter.session.info.runtime_id = 'replacement'
        self.refused('context_conflict', expected_context=prior.context)

    def test_unique_view_and_direct_list_required(self):
        self.adapter.extra = [Node('mmui::MessageView', 'duplicate', rect=self.adapter.view.bounds)]
        self.refused('history_frame_ambiguous')
        self.adapter.extra = []; self.adapter.list.parent_node = self.adapter.root_node
        self.refused('history_frame_ambiguous')

    def test_multiple_direct_lists_rejected(self):
        self.adapter.extra = [Node('mmui::RecyclerListView', 'second-list', control='List',
                                   rect=self.adapter.list.bounds, parent=self.adapter.view)]
        self.refused('history_frame_ambiguous')

    def test_current_root_and_container_geometry_required(self):
        self.adapter.view.bounds = (667, 201, 3229, 1680); self.refused('history_frame_changed')
        self.adapter.view.bounds = self.adapter.list.bounds
        self.adapter.root_node.bounds = (0, 0, 1000, 1000); self.refused('history_frame_changed')

    def test_ancestor_cycle_or_foreign_native_window_rejected(self):
        self.adapter.view.parent_node = self.adapter.view; self.refused('history_ancestry_invalid')
        foreign = Node('mmui::MainWindow', 'foreign', control='Window', handle=456)
        self.adapter.view.parent_node = foreign; self.refused('history_ancestry_invalid')

    def test_ancestor_walk_is_bounded(self):
        parent = self.adapter.root_node
        for index in range(41): parent = Node('mmui::XView', f'ancestor-{index}', parent=parent)
        self.adapter.view.parent_node = parent; self.refused('history_ancestry_invalid')

    def test_exactly_forty_parent_edges_to_root_is_accepted(self):
        parent = self.adapter.root_node
        for index in range(39): parent = Node('mmui::XView', f'ancestor-{index}', parent=parent)
        self.adapter.view.parent_node = parent
        try: view = self.observe()
        except AdapterError as exc: self.fail('40 parent edges should pass: ' + exc.code)
        self.assertEqual(len(view.rows), 3)

    def test_recheck_uses_no_second_enumeration(self):
        view = self.observe(); before = self.adapter.walks
        view.recheck(); self.assertEqual(self.adapter.walks, before)

    def test_recheck_rejects_changed_input_frame_or_draft(self):
        for change in ('input', 'frame', 'draft', 'session'):
            with self.subTest(change=change):
                self.adapter = Adapter(); view = self.observe()
                if change == 'input': self.adapter.field.info.runtime_id = 'new-field'
                elif change == 'frame': self.adapter.list.info.runtime_id = 'new-list'
                elif change == 'draft': self.adapter.field.text = 'PRIVATE draft'
                else: self.adapter.session.selected = False
                with self.assertRaises(AdapterError): view.recheck()

    def test_recheck_requires_original_session_class_and_type(self):
        for attribute, value in (('class_name', 'mmui::OtherRow'), ('control_type', 'Button')):
            self.adapter = Adapter(); view = self.observe()
            setattr(self.adapter.session.info, attribute, value)
            with self.assertRaises(AdapterError) as caught: view.recheck()
            self.assertEqual(caught.exception.code, 'context_conflict')

    def test_recheck_allows_viewport_rows_to_move_without_changing_context(self):
        view = self.observe()
        self.adapter.rows[0].bounds = (667, 0, 3229, 140)
        view.recheck()  # It validates context/frame, not stale baseline rows.

    def test_provider_failures_are_sanitized(self):
        self.adapter.rows[0].stale = True; self.refused('history_observation_failed')

    def test_row_changed_during_property_read_rejected(self):
        def change(): self.adapter.rows[0].info.name = 'PRIVATE changed'
        self.adapter.rows[0].on_parent = change
        self.refused('history_row_changed')

    def test_final_background_check_after_provider_work(self):
        def change(): self.adapter.block = 'background_requires_minimized'
        self.adapter.rows[-1].on_rect = change
        self.refused('background_requires_minimized')

    def test_invalid_selection_type_refuses(self):
        self.adapter.session.selected = 'true'; self.refused('history_observation_failed')

    def test_duplicate_input_and_no_rows_refuse(self):
        self.adapter.extra = [Node('mmui::ChatInputField', 'other', aid='chat_input_field', control='Edit')]
        self.refused('context_conflict')
        self.adapter.extra = []; self.adapter.rows = []; self.refused('history_rows_missing')


if __name__ == '__main__': unittest.main()
