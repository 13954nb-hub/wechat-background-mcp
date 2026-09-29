"""Guardian refuses unverified session viewport enumeration results."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from test_gate import FakeBackend
from wxbg import supervisor


ARGS = {'max_steps': 1, 'direction': 'down'}
EVIDENCE = {
    'direction': 'down', 'requested_steps': 1,
    'attempted_steps': 1, 'delivered_steps': 1,
    'delivery_started': True, 'activation_started': False,
    'target_found': False, 'selected_and_header_verified': False,
    'reached_end': False, 'primary_error_code': None,
}
DESKTOP = {
    'background_observation_passed': True, 'observations': 1,
    'scan_background_observation_passed': True,
    'target_foreground_observed': False,
    'foreground_changed': False, 'clipboard_changed': False,
    'cursor_changed': False, 'target_restored': False,
    'capture_observed': False, 'new_visible_windows': [],
    'monitor_errors': [],
}
BODY = {
    'ok': True, 'status': 'bounded_complete',
    'viewports': [
        {'index': 0, 'rows': [{'title': 'Example Group', 'fully_visible': True}]},
        {'index': 1, 'rows': [{'title': '別的群', 'fully_visible': True}]},
    ],
    'counts': {'wheel_steps': 1, 'viewports': 2},
    'coverage': 'bounded_ui_viewports', 'end_verified': False,
    'background_mode': 'minimized',
}


class ViewportGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = FakeBackend()
        self.backend.state_dir = Path(self.temp.name)

    def run_guardian(self, reply):
        return supervisor.run_request(
            {'action': 'scan_session_viewports', 'args': ARGS},
            timeout=supervisor.MAX_SCAN_WORKER_SECONDS,
            state_dir=Path(self.temp.name), backend=self.backend,
            worker_runner=lambda *_: deepcopy(reply),
            mutex_factory=lambda *_: nullcontext(),
        )

    def test_accepts_verified_bounded_rows_without_opening(self):
        reply = {'ok': True, 'result': BODY,
                 'navigation_evidence': EVIDENCE, 'evidence': DESKTOP}
        response = self.run_guardian(reply)
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['result'], BODY)

    def test_rejects_private_or_unverified_rows(self):
        for change in ('private', 'missing_evidence', 'background'):
            with self.subTest(change=change):
                reply = {'ok': True, 'result': deepcopy(BODY),
                         'navigation_evidence': deepcopy(EVIDENCE),
                         'evidence': deepcopy(DESKTOP)}
                if change == 'private':
                    reply['result']['viewports'][0]['rows'][0]['preview'] = 'secret'
                elif change == 'missing_evidence':
                    del reply['navigation_evidence']
                else:
                    reply['evidence']['cursor_changed'] = True
                response = self.run_guardian(reply)
                self.assertFalse(response['ok'], response)
                self.assertNotIn('result', response)
                self.assertTrue(response['error']['outcome_unknown'])
                self.assertNotIn('secret', str(response))

    def test_scan_action_has_extended_worker_budget(self):
        self.assertIn('scan_session_viewports', supervisor.SCAN_ACTIONS)


if __name__ == '__main__':
    unittest.main()
