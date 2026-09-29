"""Public contract for bounded, content-free Chats viewport enumeration."""
import unittest

from wxbg.session_viewport_contract import valid_session_viewports_success


DESKTOP = {
    'scan_background_observation_passed': True,
    'target_foreground_observed': False,
    'observations': 1,
    'foreground_changed': False,
    'cursor_changed': False,
    'clipboard_changed': False,
    'target_restored': False,
    'capture_observed': False,
    'new_visible_windows': [],
    'monitor_errors': [],
}


def evidence(steps=1, direction='down'):
    return {
        'direction': direction,
        'requested_steps': steps,
        'attempted_steps': steps,
        'delivered_steps': steps,
        'delivery_started': steps > 0,
        'activation_started': False,
        'target_found': False,
        'selected_and_header_verified': False,
        'reached_end': False,
        'primary_error_code': None,
    }


def result():
    return {
        'ok': True,
        'status': 'bounded_complete',
        'viewports': [
            {'index': 0, 'rows': [{'title': 'Example Group', 'fully_visible': True}]},
            {'index': 1, 'rows': [{'title': 'Example Group', 'fully_visible': False},
                                  {'title': '另一個群', 'fully_visible': True}]},
        ],
        'counts': {'wheel_steps': 1, 'viewports': 2},
        'coverage': 'bounded_ui_viewports',
        'end_verified': False,
        'background_mode': 'minimized',
    }


class ViewportContractTests(unittest.TestCase):
    def test_accepts_bounded_viewports_with_duplicate_titles(self):
        self.assertTrue(valid_session_viewports_success(
            result(), evidence(), DESKTOP, {'max_steps': 1, 'direction': 'down'}))

    def test_rejects_claim_of_full_coverage_or_extra_private_fields(self):
        for change in (
            {'end_verified': True},
            {'coverage': 'all_sessions'},
            {'message_preview': 'secret'},
        ):
            with self.subTest(change=change):
                self.assertFalse(valid_session_viewports_success(
                    {**result(), **change}, evidence(), DESKTOP,
                    {'max_steps': 1, 'direction': 'down'}))

    def test_rejects_bad_row_shape_and_count(self):
        for row in (
            {'title': '群', 'fully_visible': True, 'ref': 'stale'},
            {'title': '', 'fully_visible': True},
            {'title': '群', 'fully_visible': 1},
        ):
            with self.subTest(row=row):
                payload = result()
                payload['viewports'][0]['rows'] = [row]
                self.assertFalse(valid_session_viewports_success(
                    payload, evidence(), DESKTOP,
                    {'max_steps': 1, 'direction': 'down'}))
        payload = result()
        payload['counts']['viewports'] = 3
        self.assertFalse(valid_session_viewports_success(
            payload, evidence(), DESKTOP, {'max_steps': 1}))

    def test_unchanged_view_does_not_assert_end_and_requires_wheel(self):
        payload = result()
        payload['status'] = 'viewport_unchanged'
        self.assertFalse(valid_session_viewports_success(
            payload, evidence(), DESKTOP, {'max_steps': 1}))
        payload['viewports'][1]['rows'] = list(payload['viewports'][0]['rows'])
        self.assertTrue(valid_session_viewports_success(
            payload, evidence(), DESKTOP, {'max_steps': 1}))
        self.assertFalse(valid_session_viewports_success(
            payload, evidence(0), DESKTOP, {'max_steps': 0}))

    def test_rejects_background_or_evidence_mismatch(self):
        self.assertFalse(valid_session_viewports_success(
            result(), evidence(direction='up'), DESKTOP,
            {'max_steps': 1, 'direction': 'down'}))
        self.assertFalse(valid_session_viewports_success(
            result(), evidence(), {**DESKTOP, 'cursor_changed': True},
            {'max_steps': 1}))


if __name__ == '__main__':
    unittest.main()
