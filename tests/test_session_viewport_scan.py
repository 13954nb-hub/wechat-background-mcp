"""Offline acceptance for bounded, content-minimal Chats viewport enumeration."""

import unittest

from test_session_navigation import FakeAdapter, Rect
from wxbg.policy import AdapterError


class SessionViewportScanTests(unittest.TestCase):
    def test_visibility_uses_current_table_bottom_after_resize(self):
        class ResizedAdapter(FakeAdapter):
            def __init__(self):
                super().__init__([['Current', 'Clipped']])
                self.root_node._rect = Rect(520, 280, 2720, 1880)
                self.session_list._rect = Rect(670, 480, 1187, 1870)
                self.table._rect = Rect(670, 480, 1187, 1870)

            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'session_item_Current':
                        node._rect = Rect(670, 480, 1187, 642)
                    elif node.element_info.automation_id == 'session_item_Clipped':
                        node._rect = Rect(670, 1800, 1187, 1900)
                return nodes

        result = self.scan(ResizedAdapter())
        self.assertEqual(result['viewports'][0]['rows'], [
            {'title': 'Current', 'fully_visible': True},
            {'title': 'Clipped', 'fully_visible': False},
        ])

    def test_logged_in_chats_without_open_conversation_can_be_observed(self):
        class NoChatAdapter(FakeAdapter):
            def nodes(self):
                return [node for node in super().nodes()
                        if node.element_info.automation_id != 'chat_input_field']

        adapter = NoChatAdapter([['One', 'Two']])
        adapter.chat = None
        result = self.scan(adapter)
        self.assertEqual(result['status'], 'bounded_complete')
        self.assertEqual([row['title'] for row in result['viewports'][0]['rows']],
                         ['One', 'Two'])
        self.assertEqual(adapter.opened, [])

    @staticmethod
    def scan(adapter, steps=0, *, direction='down', wheel=None, deadline=None,
             clock=None):
        from wxbg.session_viewport_scan import scan_session_viewports

        kwargs = {'direction': direction, 'wheel': wheel,
                  'sleep': lambda _seconds: None}
        if deadline is not None:
            kwargs['deadline'] = deadline
        if clock is not None:
            kwargs['clock'] = clock
        return scan_session_viewports(adapter, steps, **kwargs)

    def test_zero_steps_reports_only_the_current_viewport_without_refs_or_preview(self):
        class PreviewAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.class_name == 'mmui::ChatSessionCell':
                        node.element_info.name = 'private message preview'
                return nodes

        adapter = PreviewAdapter([['Current', 'Example Group', 'Example Group']])
        result = self.scan(adapter, wheel=lambda *_args, **_kwargs:
                           self.fail('zero steps must not wheel'))

        self.assertEqual(set(result), {'ok', 'status', 'viewports', 'counts',
                                       'coverage', 'end_verified', 'background_mode'})
        self.assertEqual(result['status'], 'bounded_complete')
        self.assertEqual(result['viewports'], [{'index': 0, 'rows': [
            {'title': 'Current', 'fully_visible': True},
            {'title': 'Example Group', 'fully_visible': True},
            {'title': 'Example Group', 'fully_visible': True},
        ]}])
        self.assertEqual(result['counts'], {'wheel_steps': 0, 'viewports': 1})
        self.assertEqual(result['coverage'], 'bounded_ui_viewports')
        self.assertIs(result['end_verified'], False)
        self.assertEqual(result['background_mode'], 'minimized')
        self.assertNotIn('private message preview', str(result))
        self.assertNotIn('ref', str(result))
        self.assertEqual(adapter.chat, 'Current')
        self.assertEqual(set(adapter.navigation_evidence), {
            'direction', 'requested_steps', 'attempted_steps', 'delivered_steps',
            'delivery_started', 'activation_started', 'target_found',
            'selected_and_header_verified', 'reached_end', 'primary_error_code'})
        self.assertIs(adapter.navigation_evidence['delivery_started'], False)

    def test_each_delivered_wheel_yields_the_next_viewport_and_keeps_order(self):
        adapter = FakeAdapter([['Current', 'A'], ['B', 'A'], ['C']])
        deltas = []

        def wheel(_adapter, _table, delta, *, check_context):
            check_context()
            deltas.append(delta)
            adapter.page += 1
            check_context()

        result = self.scan(adapter, 2, wheel=wheel)

        self.assertEqual(deltas, [-120, -120])
        self.assertEqual(result['viewports'], [
            {'index': 0, 'rows': [{'title': 'Current', 'fully_visible': True},
                                  {'title': 'A', 'fully_visible': True}]},
            {'index': 1, 'rows': [{'title': 'B', 'fully_visible': True},
                                  {'title': 'A', 'fully_visible': True}]},
            {'index': 2, 'rows': [{'title': 'C', 'fully_visible': True}]},
        ])
        self.assertEqual(result['counts'], {'wheel_steps': 2, 'viewports': 3})
        self.assertEqual(adapter.navigation_evidence['delivered_steps'], 2)
        self.assertIs(adapter.navigation_evidence['activation_started'], False)
        self.assertEqual(adapter.chat, 'Current')
        self.assertEqual(adapter.opened, [])

    def test_upward_scan_uses_positive_wheel_delta(self):
        adapter = FakeAdapter([['Current'], ['Older']])
        deltas = []

        def wheel(_adapter, _table, delta, *, check_context):
            check_context()
            deltas.append(delta)
            adapter.page += 1

        result = self.scan(adapter, 1, direction='up', wheel=wheel)
        self.assertEqual(deltas, [120])
        self.assertEqual(result['viewports'][1]['rows'][0]['title'], 'Older')
        self.assertEqual(adapter.navigation_evidence['direction'], 'up')

    def test_partly_clipped_row_is_labeled_without_a_sendable_ref(self):
        class ClippedAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'session_item_Clipped':
                        node._rect = Rect(152, 1945, 669, 2107)
                return nodes

        result = self.scan(ClippedAdapter([['Current', 'Clipped']]))
        self.assertEqual(result['viewports'][0]['rows'][-1],
                         {'title': 'Clipped', 'fully_visible': False})

    def test_unchanged_viewport_is_not_reported_as_the_end(self):
        adapter = FakeAdapter([['Current', 'A']])

        def wheel(_adapter, _table, _delta, *, check_context):
            check_context()

        result = self.scan(adapter, 4, wheel=wheel)
        self.assertEqual(result['status'], 'viewport_unchanged')
        self.assertEqual(result['counts'], {'wheel_steps': 1, 'viewports': 2})
        self.assertEqual(result['viewports'][0]['rows'], result['viewports'][1]['rows'])
        self.assertIs(result['end_verified'], False)
        self.assertIs(adapter.navigation_evidence['reached_end'], False)

    def test_more_than_32_rows_rejects_before_wheel(self):
        class CrowdedAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.class_name == 'mmui::ChatSessionCell':
                        node._rect = Rect(152, 200, 669, 362)
                return nodes

        adapter = CrowdedAdapter([[f'Row{n}' for n in range(33)]])
        with self.assertRaises(AdapterError) as caught:
            self.scan(adapter, 1, wheel=lambda *_args, **_kwargs:
                      self.fail('overflow must not wheel'))
        self.assertEqual(caught.exception.code, 'session_view_overflow')
        self.assertEqual(adapter.navigation_evidence['attempted_steps'], 0)
        self.assertEqual(adapter.navigation_evidence['primary_error_code'],
                         'session_view_overflow')

    def test_nonempty_draft_rejects_before_wheel(self):
        class DraftAdapter(FakeAdapter):
            def nodes(self):
                nodes = super().nodes()
                for node in nodes:
                    if node.element_info.automation_id == 'chat_input_field':
                        node.iface_value.CurrentValue = 'n'
                return nodes

        adapter = DraftAdapter([['Current']])
        with self.assertRaises(AdapterError) as caught:
            self.scan(adapter, 1, wheel=lambda *_args, **_kwargs:
                      self.fail('draft conflict must not wheel'))
        self.assertEqual(caught.exception.code, 'context_conflict')
        self.assertEqual(adapter.navigation_evidence['attempted_steps'], 0)

    def test_changed_chat_after_wheel_is_rejected(self):
        adapter = FakeAdapter([['Current'], ['Other']])

        def wheel(_adapter, _table, _delta, *, check_context):
            check_context()
            adapter.chat = 'Other'
            adapter.page += 1

        with self.assertRaises(AdapterError) as caught:
            self.scan(adapter, 1, wheel=wheel)
        self.assertEqual(caught.exception.code, 'context_conflict')
        self.assertIs(adapter.navigation_evidence['delivery_started'], True)
        self.assertEqual(adapter.navigation_evidence['attempted_steps'], 1)
        self.assertEqual(adapter.navigation_evidence['primary_error_code'],
                         'context_conflict')

    def test_strict_arguments_and_deadline_reserve_prevent_wheel(self):
        for steps, direction, expected in ((True, 'down', 'invalid_scroll_steps'),
                                           (5, 'down', 'invalid_scroll_steps'),
                                           (1, 'sideways', 'invalid_scan_direction')):
            with self.subTest(steps=steps, direction=direction):
                adapter = FakeAdapter([['Current']])
                with self.assertRaises(AdapterError) as caught:
                    self.scan(adapter, steps, direction=direction,
                              wheel=lambda *_args, **_kwargs:
                              self.fail('invalid request must not wheel'))
                self.assertEqual(caught.exception.code, expected)

        adapter = FakeAdapter([['Current']])
        with self.assertRaises(AdapterError) as caught:
            self.scan(adapter, 1, direction='up', deadline=9.0,
                      clock=lambda: 0.0,
                      wheel=lambda *_args, **_kwargs:
                      self.fail('insufficient budget must not wheel'))
        self.assertEqual(caught.exception.code, 'session_scan_budget_exhausted')
        self.assertEqual(adapter.navigation_evidence['attempted_steps'], 0)


if __name__ == '__main__':
    unittest.main()
