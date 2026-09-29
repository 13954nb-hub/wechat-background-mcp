"""Pure public event protocol tests; no provider, process or UI is contacted."""
from copy import deepcopy
import unittest

try:
    from wxbg import event_contract as contract
except ImportError:
    contract = None


def event_evidence():
    return {'started': True, 'armed': True, 'auto_focus_disabled': True,
            'owner_mta': True, 'owner_windowless': True, 'identity_verified': True,
            'registrations_removed': True, 'handler_refs_released': True,
            'owner_exited': True, 'cleanup_pending': False, 'startup_dropped': 0,
            'invalid_dropped': 0, 'primary_error_code': None, 'cleanup_error_code': None}


def hint_result(hint=False, duration=1):
    return {'status': 'hint_observed' if hint else 'timed_out',
            'scope': 'main_window_subtree', 'observed_kinds': ['text_changed'] if hint else [],
            'counts': {'notification': 0, 'text_changed': 1 if hint else 0,
                       'buffered': 1 if hint else 0, 'overflow_dropped': 0, 'after_close_dropped': 0},
            'requested_wait_ms': duration*1000, 'armed_window_ms': 100 if hint else duration*1000,
            'coverage': {'continuous': False, 'complete': False, 'recipient_known': False, 'missed_count': None}}


class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(contract, 'typed event protocol has not been implemented')

    def test_quiet_and_real_hint_have_valid_fixed_contracts(self):
        for hint in (False, True):
            self.assertTrue(contract.valid_hint_success(hint_result(hint), event_evidence(),
                {'background_observation_passed': True}, 1))

    def test_unknown_cleanup_is_never_proved_by_a_valid_result(self):
        unknown = contract.unknown_event_evidence()
        self.assertIsNone(unknown['registrations_removed'])
        self.assertIsNone(unknown['owner_exited'])
        self.assertFalse(contract.valid_hint_success(hint_result(), unknown,
            {'background_observation_passed': True}, 1))

    def test_cleanup_flags_must_be_actual_booleans_and_all_proved(self):
        for key in ('started','armed','auto_focus_disabled','owner_mta','owner_windowless',
                    'identity_verified','registrations_removed','handler_refs_released','owner_exited'):
            for bad in (False, None, 1, 'true'):
                value = event_evidence(); value[key] = bad
                self.assertFalse(contract.valid_hint_success(hint_result(), value,
                    {'background_observation_passed': True}, 1), (key, bad))

    def test_pending_cleanup_or_known_error_cannot_succeed(self):
        for key, value in (('cleanup_pending', True), ('cleanup_pending', 0),
                           ('primary_error_code', 'event_open_failed'), ('cleanup_error_code', 'remove_failed')):
            evidence = event_evidence(); evidence[key] = value
            self.assertFalse(contract.valid_hint_success(hint_result(), evidence,
                {'background_observation_passed': True}, 1))

    def test_missing_or_failed_background_evidence_cannot_succeed(self):
        for background in (None, {}, {'background_observation_passed': False},
                           {'background_observation_passed': 1}):
            self.assertFalse(contract.valid_hint_success(hint_result(), event_evidence(), background, 1))

    def test_short_quiet_window_is_not_a_complete_timeout(self):
        result = hint_result(); result['armed_window_ms'] = 999
        self.assertFalse(contract.valid_hint_result(result, 1))

    def test_hint_requires_positive_counts_and_matching_kinds(self):
        for field, value in (('status','hint_observed'), ('observed_kinds',['text_changed'])):
            result = hint_result(); result[field] = value
            self.assertFalse(contract.valid_hint_result(result, 1))
        result = hint_result(True); result['observed_kinds'] = ['notification']
        self.assertFalse(contract.valid_hint_result(result, 1))

    def test_counts_are_strict_and_bounded_buffer_matches_received(self):
        for bad in (True, -1, 1.0, '1'):
            result = hint_result(True); result['counts']['text_changed'] = bad
            self.assertFalse(contract.valid_hint_result(result, 1))
        result = hint_result(True); result['counts']['buffered'] = 0
        self.assertFalse(contract.valid_hint_result(result, 1))
        result = hint_result(True); result['counts'].update(text_changed=129, buffered=129)
        self.assertFalse(contract.valid_hint_result(result, 1))
        result['counts'].update(buffered=128, overflow_dropped=1)
        self.assertTrue(contract.valid_hint_result(result, 1))

    def test_late_callbacks_do_not_turn_quiet_into_hint(self):
        result = hint_result(); result['counts']['after_close_dropped'] = 7
        self.assertTrue(contract.valid_hint_result(result, 1))

    def test_false_coverage_or_extra_text_payload_is_rejected(self):
        for key, value in (('complete',True),('continuous',True),('recipient_known',True),('missed_count',0)):
            result = hint_result(); result['coverage'][key] = value
            self.assertFalse(contract.valid_hint_result(result, 1))
        result = hint_result(); result['text'] = 'synthetic provider private text'
        self.assertFalse(contract.valid_hint_result(result, 1))

    def test_normalizer_drops_untrusted_fields_and_rejects_bad_types(self):
        evidence = event_evidence(); evidence['provider_text'] = 'must not leak'
        cleaned, valid = contract.normalize_event_evidence(evidence)
        self.assertFalse(valid)
        self.assertNotIn('provider_text', cleaned)
        evidence = event_evidence(); evidence['cleanup_error_code'] = 'private sentence with spaces'
        cleaned, valid = contract.normalize_event_evidence(evidence)
        self.assertFalse(valid)
        self.assertNotIn('private sentence', str(cleaned))

    def test_duration_range_and_cross_request_budget_are_strict(self):
        for invalid in (True, 0, 16, 1.0, '1'):
            self.assertFalse(contract.valid_hint_result(hint_result(), invalid))
        self.assertFalse(contract.valid_hint_result(hint_result(duration=3), 1))


if __name__ == '__main__':
    unittest.main()
