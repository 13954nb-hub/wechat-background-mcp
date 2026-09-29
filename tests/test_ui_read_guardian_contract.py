"""Synthetic cross-process UI read contracts; never touches Weixin."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest

from test_gate import FakeBackend
from wxbg import supervisor


STATUS = {
    'status': 'ready', 'pid': 222, 'current_chat': 'Alice',
    'strict_background': True, 'auto_set_focus': False,
    'account_identity_verified': False,
    'layout_calibration': {
        'root_rect': [0, 0, 3240, 1980],
        'render_client': [0, 0, 3240, 1980],
        'window_dpi': 144,
        'coordinate_mapping_verified': True,
        'send_button': {'rect': [3060, 1862, 3180, 1927],
                        'client_point': [3120, 1894]},
        'attachment_button': None,
        'session_table': {'rect': [150, 200, 750, 1970],
                          'client_point': [450, 1085]},
    },
}
SESSIONS = {
    'sessions': [{'ref': 'a' * 32, 'title': 'Alice'}],
    'visible_only': True, 'count': 1, 'current_chat': 'Alice',
}
MESSAGES = {
    'chat': 'Alice',
    'messages': [{'ref': 'b' * 32, 'type': 'mmui::ChatTextItemView', 'text': 'hello'}],
    'visible_only': True, 'not_full_history': True,
}
CONTACTS = {
    'contacts': [{'contact_ref': 'c' * 32, 'display_text': 'Alice'}],
    'query': '', 'limit': 100, 'count': 1, 'exposed_count': 1,
    'visible_only': True, 'not_full_directory': True,
    'pagination_supported': False, 'contact_ref_scope': 'temporary_ui_contacts',
    'query_scope': 'exposed_contacts_only', 'background_mode': 'minimized',
    'scroll_steps': 0, 'bounded_scroll_supported': True,
    'scroll_steps_limit': 24, 'original_contacts_view_restored': True,
    'original_conversation_restored': True,
}
OPEN_REF = 'd' * 32
OPEN_RESULT = {
    'title': 'Alice', 'status': 'opened',
    'verification_level': 'client_ui', 'background_mode': 'minimized',
}
OPEN_EVIDENCE = {
    'session_ref': OPEN_REF, 'title': 'Alice',
    'activation_started': True, 'selected_and_header_verified': True,
}
DESKTOP = {
    'background_observation_passed': True, 'observations': 1,
    'foreground_changed': False, 'clipboard_changed': False,
    'target_restored': False, 'capture_observed': False,
    'cursor_changed': False,
    'new_visible_windows': [], 'monitor_errors': [],
    'before': {
        'foreground': 10, 'clipboard_sequence': 20, 'minimized': True,
        'visible_windows': [444], 'capture': 0, 'cursor': [30, 40],
        'cursor_api': 'GetCursorPos', 'cursor_dpi_context': 'per_monitor_v2',
        'cursor_coordinate_space': 'screen_coordinates_under_pm_v2',
    },
    'after': {
        'foreground': 10, 'clipboard_sequence': 20, 'minimized': True,
        'visible_windows': [444], 'capture': 0, 'cursor': [30, 40],
        'cursor_api': 'GetCursorPos', 'cursor_dpi_context': 'per_monitor_v2',
        'cursor_coordinate_space': 'screen_coordinates_under_pm_v2',
    },
}


class BasicUIReadGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.state_dir = self.state

    def run_reply(self, action, args, reply):
        return supervisor.run_request(
            {'action': action, 'args': args}, state_dir=self.state,
            backend=self.backend, worker_runner=lambda *_: reply,
            mutex_factory=lambda *_: nullcontext(),
        )

    def test_verified_bounded_results_remain_usable(self):
        for action, args, body in (
            ('status', {}, STATUS),
            ('list_sessions', {'query': '', 'limit': 100}, SESSIONS),
            ('read_messages', {'limit': 50}, MESSAGES),
            ('list_contacts', {'query': '', 'limit': 100, 'scroll_steps': 0}, CONTACTS),
        ):
            with self.subTest(action=action):
                reply = {'ok': True, 'result': body, 'evidence': DESKTOP}
                if action == 'list_contacts':
                    reply['navigation_evidence'] = {'restored': True}
                value = self.run_reply(action, args, reply)
                self.assertTrue(value['ok'], value)
                self.assertEqual(value['result'], body)

    def test_status_rejects_inconsistent_calibrated_render_size(self):
        invalid = {**STATUS, 'layout_calibration': {
            **STATUS['layout_calibration'], 'render_client': [0, 0, 3240, 1979],
        }}
        value = self.run_reply('status', {},
                               {'ok': True, 'result': invalid, 'evidence': DESKTOP})
        self.assertFalse(value['ok'], value)

    def test_missing_or_failed_desktop_evidence_rejects_success(self):
        for action, args, body in (
            ('status', {}, STATUS),
            ('list_sessions', {'query': '', 'limit': 100}, SESSIONS),
            ('read_messages', {'limit': 50}, MESSAGES),
            ('list_contacts', {'query': '', 'limit': 100, 'scroll_steps': 0}, CONTACTS),
        ):
            for desktop in (None, {'background_observation_passed': False}):
                with self.subTest(action=action, desktop=desktop):
                    reply = {'ok': True, 'result': body}
                    if desktop is not None:
                        reply['evidence'] = desktop
                    if action == 'list_contacts':
                        reply['navigation_evidence'] = {'restored': True}
                    value = self.run_reply(action, args, reply)
                    self.assertFalse(value['ok'], value)
                    self.assertNotIn('result', value)
                    self.assertTrue(value['error']['outcome_unknown'])

    def test_contradictory_desktop_snapshots_reject_success(self):
        for desktop in (
            {key: value for key, value in DESKTOP.items() if key != 'before'},
            {**DESKTOP, 'after': {**DESKTOP['after'], 'foreground': 99}},
        ):
            with self.subTest(desktop=desktop):
                reply = {'ok': True, 'result': STATUS, 'evidence': desktop}
                value = self.run_reply('status', {}, reply)
                self.assertFalse(value['ok'], value)
                self.assertNotIn('result', value)

    def test_inconsistent_visible_counts_reject_success(self):
        for action, args, body, key in (
            ('list_sessions', {'query': '', 'limit': 100}, SESSIONS, 'count'),
            ('list_contacts', {'query': '', 'limit': 100, 'scroll_steps': 0}, CONTACTS, 'count'),
        ):
            with self.subTest(action=action):
                result = {**body, key: 2}
                reply = {'ok': True, 'result': result, 'evidence': DESKTOP}
                if action == 'list_contacts':
                    reply['navigation_evidence'] = {'restored': True}
                value = self.run_reply(action, args, reply)
                self.assertFalse(value['ok'], value)
                self.assertNotIn('result', value)

    def test_contact_failure_without_restoration_proof_is_unknown(self):
        for navigation in (None, {'attempted': True, 'entered': True,
                                  'restore_attempted': True, 'restored': False}):
            with self.subTest(navigation=navigation):
                reply = {'ok': False, 'error': {'code': 'contacts_not_opened',
                                               'submission_started': False},
                         'evidence': {'background_observation_passed': True}}
                if navigation is not None:
                    reply['navigation_evidence'] = navigation
                value = self.run_reply('list_contacts', {'scroll_steps': 1}, reply)
                self.assertFalse(value['ok'])
                self.assertTrue(value['error']['outcome_unknown'], value)

    def test_open_session_requires_background_and_selection_proof(self):
        for evidence, desktop in (
            (None, DESKTOP),
            (OPEN_EVIDENCE, {'background_observation_passed': False}),
            ({**OPEN_EVIDENCE, 'selected_and_header_verified': False}, DESKTOP),
        ):
            with self.subTest(evidence=evidence, desktop=desktop):
                reply = {'ok': True, 'result': OPEN_RESULT, 'evidence': desktop}
                if evidence is not None:
                    reply['navigation_evidence'] = evidence
                value = self.run_reply('open_session', {'session_ref': OPEN_REF}, reply)
                self.assertFalse(value['ok'], value)
                self.assertNotIn('result', value)
                self.assertTrue(value['error']['outcome_unknown'])

        reply = {'ok': True, 'result': OPEN_RESULT,
                 'navigation_evidence': OPEN_EVIDENCE, 'evidence': DESKTOP}
        value = self.run_reply('open_session', {'session_ref': OPEN_REF}, reply)
        self.assertTrue(value['ok'], value)

    def test_open_session_failure_after_click_is_unknown(self):
        reply = {'ok': False, 'error': {'code': 'session_not_opened',
                                       'submission_started': False},
                 'navigation_evidence': {**OPEN_EVIDENCE,
                                         'selected_and_header_verified': False},
                 'evidence': DESKTOP}
        value = self.run_reply('open_session', {'session_ref': OPEN_REF}, reply)
        self.assertFalse(value['ok'])
        self.assertTrue(value['error']['outcome_unknown'], value)


if __name__ == '__main__':
    unittest.main()
