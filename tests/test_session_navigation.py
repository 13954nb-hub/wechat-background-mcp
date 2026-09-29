"""Fake UIA tests for bounded background session scanning and opening."""
import unittest
from types import SimpleNamespace

from wxbg.policy import AdapterError


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class Node:
    def __init__(self, kind, control, rect, *, aid='', name='', parent=None,
                 runtime=None, selected=False, value=None):
        self.element_info = SimpleNamespace(
            class_name=kind, control_type=control, automation_id=aid,
            name=name, runtime_id=runtime or aid or kind)
        self._rect = Rect(*rect)
        self._parent = parent
        self.iface_selection_item = SimpleNamespace(CurrentIsSelected=selected)
        self.iface_value = SimpleNamespace(CurrentValue=value)

    def rectangle(self):
        return self._rect

    def parent(self):
        return self._parent


class FakeAdapter:
    def __init__(self, pages):
        self.pages = pages
        self.page = 0
        self.identity = 'fake:1'
        self.chat = 'Current'
        self.opened = []
        self.navigation_evidence = None
        self.root_node = Node('mmui::MainWindow', 'Window', (2, 0, 3237, 1995))
        self.session_list = Node('mmui::ChatSessionList', 'Group',
                                 (152, 200, 669, 1985), parent=self.root_node)
        self.table = Node('mmui::XTableView', 'List',
                          (152, 200, 669, 1985), parent=self.session_list,
                          runtime='table')

    def precondition(self):
        pass

    def root(self):
        return self.root_node

    def nodes(self):
        field = Node('mmui::ChatInputField', 'Edit', (680, 1700, 3200, 1900),
                     aid='chat_input_field', name=self.chat, value='')
        rows = [Node('mmui::ChatSessionCell', 'ListItem',
                     (152, 200 + n * 162, 669, 362 + n * 162),
                     aid='session_item_' + title, parent=self.table,
                     runtime=f'{self.page}-{n}', selected=(title == self.chat))
                for n, title in enumerate(self.pages[self.page])]
        return [self.session_list, self.table, field, *rows]

    def ref(self, node):
        return node.element_info.runtime_id + ':' + node.element_info.automation_id

    def open_session(self, session_ref):
        rows = [node for node in self.nodes()
                if node.element_info.class_name == 'mmui::ChatSessionCell']
        matches = [row for row in rows if self.ref(row) == session_ref]
        if len(matches) != 1:
            raise AdapterError('not_found')
        self.chat = matches[0].element_info.automation_id.removeprefix('session_item_')
        self.opened.append(self.chat)
        return {'status': 'opened', 'title': self.chat}

    def selected_target(self, session_ref):
        return any(self.ref(row) == session_ref and row.iface_selection_item.CurrentIsSelected
                   for row in self.nodes()
                   if row.element_info.class_name == 'mmui::ChatSessionCell')

    def current_chat(self):
        return self.chat


class RealOpenAdapter(FakeAdapter):
    """Exercise the production open path against fake UIA rows."""

    def open_session(self, session_ref):
        from wxbg.adapter import Adapter
        return Adapter.open_session(self, session_ref)

    def click(self, _node):
        raise AdapterError('session_not_opened')


class SessionNavigationTests(unittest.TestCase):
    def test_scan_uses_live_window_and_table_bounds_after_resize(self):
        from wxbg.session_navigation import _observe, scan_open_session

        class ResizedAdapter(FakeAdapter):
            def __init__(self):
                super().__init__([['Current', 'Other']])
                self.root_node._rect = Rect(520, 280, 2720, 1880)
                self.session_list._rect = Rect(670, 480, 1187, 1870)
                self.table._rect = Rect(670, 480, 1187, 1870)

            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.class_name == 'mmui::ChatSessionCell':
                        rect = node._rect
                        node._rect = Rect(rect.left + 518, rect.top + 280,
                                          rect.right + 518, rect.bottom + 280)
                return nodes

        adapter = ResizedAdapter()
        view = _observe(adapter, 'Current')
        self.assertEqual(view['table_rect'], (670, 480, 1187, 1870))
        result = scan_open_session(adapter, 'Current', 0,
                                   wheel=lambda *args, **kwargs: None)
        self.assertEqual(result['status'], 'opened')

    def test_selected_target_real_open_preserves_scan_evidence(self):
        from wxbg.session_scan_contract import normalize_scan_evidence
        from wxbg.session_navigation import scan_open_session

        adapter = RealOpenAdapter([['Example Group', 'Other']])
        adapter.chat = 'Example Group'
        result = scan_open_session(adapter, 'Example Group', 0,
                                   wheel=lambda *args, **kwargs: None)

        self.assertEqual(result['status'], 'opened')
        self.assertEqual(result['counts'], {'wheel_steps': 0, 'viewports': 1})
        self.assertEqual(adapter.navigation_evidence['direction'], 'down')
        self.assertEqual(adapter.navigation_evidence['requested_steps'], 0)
        self.assertEqual(adapter.navigation_evidence['delivered_steps'], 0)
        self.assertTrue(adapter.navigation_evidence['target_found'])
        self.assertTrue(adapter.navigation_evidence['selected_and_header_verified'])
        self.assertFalse(adapter.navigation_evidence['activation_started'])
        self.assertTrue(normalize_scan_evidence(adapter.navigation_evidence)[1])

    def test_real_open_failure_preserves_scan_attempt_evidence(self):
        from wxbg.session_scan_contract import normalize_scan_evidence
        from wxbg.session_navigation import scan_open_session

        adapter = RealOpenAdapter([['Current', 'Example Group']])
        with self.assertRaisesRegex(AdapterError, 'session_not_opened'):
            scan_open_session(adapter, 'Example Group', 0,
                              wheel=lambda *args, **kwargs: None)

        self.assertEqual(adapter.navigation_evidence['direction'], 'down')
        self.assertEqual(adapter.navigation_evidence['requested_steps'], 0)
        self.assertTrue(adapter.navigation_evidence['target_found'])
        self.assertTrue(adapter.navigation_evidence['activation_started'])
        self.assertFalse(adapter.navigation_evidence['selected_and_header_verified'])
        self.assertEqual(adapter.navigation_evidence['primary_error_code'],
                         'session_not_opened')
        self.assertTrue(normalize_scan_evidence(adapter.navigation_evidence)[1])

    def test_one_wheel_finds_exact_group_and_verifies_open(self):
        from wxbg.session_navigation import scan_open_session

        adapter = FakeAdapter([['Current', 'Other'], ['Example Group', 'Another']])
        deltas = []

        def wheel(_adapter, _table, delta, *, check_context):
            check_context()
            deltas.append(delta)
            adapter.page += 1
            check_context()

        result = scan_open_session(adapter, 'Example Group', 4, wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(deltas, [-120])
        self.assertEqual(adapter.opened, ['Example Group'])
        self.assertEqual(result['status'], 'opened')
        self.assertEqual(result['counts'], {'wheel_steps': 1, 'viewports': 2})
        self.assertTrue(adapter.navigation_evidence['selected_and_header_verified'])

    def test_four_step_budget_returns_bounded_miss(self):
        from wxbg.session_navigation import scan_open_session

        adapter = FakeAdapter([['Current'], ['A'], ['B'], ['C'], ['D']])

        def wheel(_adapter, _table, _delta, *, check_context):
            check_context()
            adapter.page += 1

        result = scan_open_session(adapter, 'Example Group', 4, wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(result['status'], 'not_found_in_bounded_scan')
        self.assertEqual(result['counts'], {'wheel_steps': 4, 'viewports': 5})
        self.assertEqual(adapter.opened, [])

    def test_invalid_steps_fail_before_navigation(self):
        from wxbg.session_navigation import scan_open_session

        adapter = FakeAdapter([['Current']])
        with self.assertRaisesRegex(AdapterError, 'invalid_scroll_steps'):
            scan_open_session(adapter, 'Example Group', 5, wheel=lambda *args, **kwargs: None)
        self.assertEqual(adapter.opened, [])

    def test_upward_scan_uses_positive_wheel_delta(self):
        from wxbg.session_navigation import scan_open_session

        adapter = FakeAdapter([['Current', 'Other'], ['Example Group', 'Current']])
        deltas = []
        def wheel(_adapter, _table, delta, *, check_context):
            check_context()
            deltas.append(delta)
            adapter.page += 1

        result = scan_open_session(adapter, 'Example Group', 1,
                                   direction='up', wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(deltas, [120])
        self.assertEqual(result['status'], 'opened')
        self.assertEqual(adapter.navigation_evidence['direction'], 'up')

    def test_invalid_direction_fails_before_any_navigation(self):
        from wxbg.session_navigation import scan_open_session

        adapter = FakeAdapter([['Current']])
        with self.assertRaisesRegex(AdapterError, 'invalid_scan_direction'):
            scan_open_session(adapter, 'Example Group', 1, direction='sideways',
                              wheel=lambda *args, **kwargs: None)
        self.assertEqual(adapter.opened, [])

    def test_unchanged_view_does_not_claim_list_end(self):
        from wxbg.session_navigation import scan_open_session

        adapter = FakeAdapter([['Current', 'Other']])

        def wheel(_adapter, _table, _delta, *, check_context):
            check_context()

        result = scan_open_session(adapter, 'Example Group', 1, wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(result['status'], 'viewport_unchanged')
        self.assertFalse(adapter.navigation_evidence['reached_end'])
        self.assertEqual(result['counts'], {'wheel_steps': 1, 'viewports': 2})

    def test_partly_clipped_exact_target_is_not_clicked(self):
        from wxbg.session_navigation import scan_open_session

        class ClippedAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'session_item_Example Group':
                        node._rect = Rect(152, 1950, 669, 2112)
                return nodes

        adapter = ClippedAdapter([['Current', 'Example Group']])
        result = scan_open_session(adapter, 'Example Group', 0,
                                   wheel=lambda *args, **kwargs: None)
        self.assertEqual(result['status'], 'target_partially_visible')
        self.assertEqual(result['counts'], {'wheel_steps': 0, 'viewports': 1})
        self.assertEqual(adapter.opened, [])
        self.assertTrue(adapter.navigation_evidence['target_found'])
        self.assertFalse(adapter.navigation_evidence['activation_started'])

    def test_bottom_clipped_target_scrolls_then_opens_only_when_inside(self):
        from wxbg.session_navigation import scan_open_session

        class MovingAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'session_item_Example Group':
                        node._rect = (Rect(152, 1945, 669, 2107) if self.page == 0
                                      else Rect(152, 1782, 669, 1944))
                return nodes

        adapter = MovingAdapter([['Current', 'Example Group'],
                                 ['Current', 'Example Group']])
        def wheel(_adapter, _table, _delta, *, check_context):
            check_context()
            adapter.page += 1

        result = scan_open_session(adapter, 'Example Group', 1, wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(result['status'], 'opened')
        self.assertEqual(result['counts'], {'wheel_steps': 1, 'viewports': 2})
        self.assertEqual(adapter.opened, ['Example Group'])

    def test_bottom_clipped_target_remains_partial_after_budget(self):
        from wxbg.session_navigation import scan_open_session

        class MovingAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'session_item_Example Group':
                        node._rect = (Rect(152, 1945, 669, 2107) if self.page == 0
                                      else Rect(152, 1900, 669, 2062))
                return nodes

        adapter = MovingAdapter([['Current', 'Example Group'],
                                 ['Current', 'Example Group']])
        def wheel(_adapter, _table, _delta, *, check_context):
            check_context()
            adapter.page += 1

        result = scan_open_session(adapter, 'Example Group', 1, wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(result['status'], 'target_partially_visible')
        self.assertEqual(result['counts'], {'wheel_steps': 1, 'viewports': 2})
        self.assertEqual(adapter.opened, [])
        self.assertTrue(adapter.navigation_evidence['target_found'])
        self.assertFalse(adapter.navigation_evidence['activation_started'])

    def test_top_clipped_target_moves_inside_only_when_scanning_up(self):
        from wxbg.session_navigation import scan_open_session

        class MovingAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'session_item_Example Group':
                        node._rect = (Rect(152, 157, 669, 319) if self.page == 0
                                      else Rect(152, 200, 669, 362))
                return nodes

        adapter = MovingAdapter([['Current', 'Example Group'],
                                 ['Current', 'Example Group']])
        deltas = []
        def wheel(_adapter, _table, delta, *, check_context):
            check_context()
            deltas.append(delta)
            adapter.page += 1

        result = scan_open_session(adapter, 'Example Group', 1,
                                   direction='up', wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(deltas, [120])
        self.assertEqual(result['status'], 'opened')

        down_adapter = MovingAdapter([['Current', 'Example Group'],
                                      ['Current', 'Example Group']])
        down_result = scan_open_session(down_adapter, 'Example Group', 1,
                                        direction='down', wheel=wheel,
                                        sleep=lambda _seconds: None)
        self.assertEqual(down_result['status'], 'target_partially_visible')
        self.assertEqual(down_adapter.page, 0)

    def test_adjacent_offscreen_row_is_ignored_until_scrolled_inside(self):
        from wxbg.session_navigation import scan_open_session

        class AdjacentAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                if self.page == 0:
                    for node in nodes:
                        if node.element_info.automation_id == 'session_item_Example Group':
                            node._rect = Rect(152, 37, 669, 199)
                return nodes

        adapter = AdjacentAdapter([['Example Group', 'Current'],
                                   ['Example Group', 'Current']])
        def wheel(_adapter, _table, delta, *, check_context):
            check_context()
            self.assertEqual(delta, 120)
            adapter.page += 1

        result = scan_open_session(adapter, 'Example Group', 1,
                                   direction='up', wheel=wheel,
                                   sleep=lambda _seconds: None)
        self.assertEqual(result['status'], 'opened')
        self.assertEqual(result['counts'], {'wheel_steps': 1, 'viewports': 2})

    def test_far_offscreen_row_remains_an_error(self):
        from wxbg.session_navigation import scan_open_session

        class FarAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'session_item_Other':
                        node._rect = Rect(152, -500, 669, -338)
                return nodes

        adapter = FarAdapter([['Current', 'Other']])
        with self.assertRaisesRegex(AdapterError, 'session_row_outside_table'):
            scan_open_session(adapter, 'Example Group', 1,
                              direction='up', wheel=lambda *args, **kwargs: None)
        self.assertEqual(adapter.navigation_evidence['attempted_steps'], 0)

    def test_guardian_deadline_reserve_refuses_wheel_before_delivery(self):
        from wxbg.session_navigation import scan_open_session

        adapter = FakeAdapter([['Current', 'Other']])
        wheel_calls = []
        with self.assertRaisesRegex(AdapterError, 'session_scan_budget_exhausted'):
            scan_open_session(adapter, 'Example Group', 1,
                              direction='up', deadline=9.0,
                              clock=lambda: 0.0,
                              wheel=lambda *args, **kwargs: wheel_calls.append(1),
                              sleep=lambda _seconds: None)
        self.assertEqual(wheel_calls, [])
        self.assertEqual(adapter.navigation_evidence['attempted_steps'], 0)
        self.assertEqual(adapter.navigation_evidence['direction'], 'up')


if __name__ == '__main__':
    unittest.main()
