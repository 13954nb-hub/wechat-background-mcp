"""Intentional history navigation using owned fakes only; no live UI calls."""
import importlib
import json
import math
import unittest
from types import SimpleNamespace

from wxbg.journal import _summary_json
from wxbg.policy import AdapterError


REF = 'a' * 32
EVIDENCE_KEYS = {
    'mode', 'direction', 'requested_steps', 'delivery_started', 'completed_steps',
    'viewport_settled', 'conversation_preserved', 'draft_preserved',
    'viewport_changed', 'primary_error_code', 'original_viewport_restoration_requested',
    'not_full_history', 'boundary_verified',
}


class FakeView:
    def __init__(self, fake):
        self.fake = fake
        self.context = fake.context
        self.session_ref = fake.ref
        self.frame_identity = fake.frame
        self.row_identity = (f'row-{fake.position}',)
        self.signature = (('mmui::ChatTextItemView', 'owned private text', fake.position),)
        self.rows = tuple(SimpleNamespace(text='owned private text') for _ in range(2 + abs(fake.position)))
        self.list_node = object()

    def recheck(self):
        f = self.fake
        f.rechecks += 1
        if f.on_recheck:
            f.on_recheck(self)
        if self.context != f.context or self.session_ref != f.ref:
            raise AdapterError('context_conflict')
        if self.frame_identity != f.frame:
            raise AdapterError('history_frame_changed')
        if f.draft:
            raise AdapterError('draft_conflict')


class Fake:
    def __init__(self):
        self.context = ('session_item_Owned', 'session-runtime', 'field-runtime')
        self.ref = REF
        self.frame = ('message-view', 'list')
        self.draft = ''
        self.position = 0
        self.time = 0.0
        self.observations = []
        self.moves = []
        self.rechecks = 0
        self.on_observe = None
        self.on_recheck = None
        self.on_wheel = None
        self.unchanged = False
        self.navigation_started = False
        self.submission_started = False

    def observe(self, adapter, *, expected_context=None):
        if adapter is not self:
            raise AssertionError('wrong adapter')
        self.observations.append(expected_context)
        if self.on_observe:
            self.on_observe(len(self.observations))
        if expected_context is not None and expected_context != self.context:
            raise AdapterError('context_conflict')
        view = FakeView(self)
        view.recheck()
        return view

    def wheel(self, adapter, node, delta, *, check_context):
        check_context()
        if self.on_wheel:
            self.on_wheel('before', delta)
        self.moves.append(delta)
        if not self.unchanged:
            self.position += 1 if delta == 120 else -1
        if self.on_wheel:
            self.on_wheel('after', delta)
        try:
            check_context()
        except Exception as exc:
            exc.wheel_delivery_started = True
            raise

    def sleep(self, seconds):
        self.time += seconds


class HistoryActionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('wxbg.history_actions')
        except ModuleNotFoundError:
            self.module = None
        self.assertIsNotNone(self.module, 'history_actions implementation is missing')
        self.fake = Fake()

    def run_action(self, direction='older', steps=1, session_ref=REF, deadline=30):
        f = self.fake
        return self.module.scroll_messages(f, session_ref, direction, steps, deadline,
            observe=f.observe, wheel=f.wheel, clock=lambda: f.time, sleep=f.sleep)

    def failure(self, code, **kwargs):
        with self.assertRaises(AdapterError) as caught:
            self.run_action(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(set(self.fake.history_evidence), EVIDENCE_KEYS)
        self.assertEqual(self.fake.history_evidence['primary_error_code'], code)
        self.assertEqual(caught.exception.outcome_unknown, self.fake.navigation_started)
        self.assertFalse(self.fake.submission_started)
        return caught.exception

    def test_invalid_steps_fail_before_observation(self):
        for steps in (True, False, 0, 5, -1, 1.0, '1', None):
            with self.subTest(steps=steps):
                self.fake = Fake()
                self.failure('invalid_scroll_steps', steps=steps)
                self.assertEqual(self.fake.observations, [])
                self.assertEqual(self.fake.moves, [])

    def test_direction_is_exact_and_invalid_input_not_copied_to_evidence(self):
        for direction in ('Older', ' older', 'newer ', 'private arbitrary content', None, 1):
            with self.subTest(direction=direction):
                self.fake = Fake()
                self.failure('invalid_scroll_direction', direction=direction)
                self.assertEqual(self.fake.observations, [])
                self.assertIsNone(self.fake.history_evidence['direction'])

    def test_invalid_ref_before_observation(self):
        for ref in ('', None, 123):
            with self.subTest(ref=ref):
                self.fake = Fake()
                self.failure('invalid_session_ref', session_ref=ref)
                self.assertEqual(self.fake.observations, [])

    def test_nonfinite_or_wrong_type_deadline_before_observation(self):
        for deadline in (True, '30', None, math.inf, math.nan):
            with self.subTest(deadline=deadline):
                self.fake = Fake()
                self.failure('invalid_deadline', deadline=deadline)
                self.assertEqual(self.fake.observations, [])

    def test_selected_ref_must_equal_requested_ref_without_opening(self):
        self.failure('context_conflict', session_ref='b' * 32)
        self.assertEqual(self.fake.moves, [])

    def test_ambiguous_initial_context_fails_without_input(self):
        def duplicate(_):
            raise AdapterError('ambiguous_recipient', 'private title')
        self.fake.on_observe = duplicate
        error = self.failure('ambiguous_recipient')
        self.assertEqual(self.fake.moves, [])
        self.assertNotIn('private title', str(error))

    def test_changed_context_during_initial_pair_is_rejected(self):
        self.fake.on_observe = lambda n: setattr(self.fake, 'context', ('changed',)) if n == 2 else None
        self.failure('context_conflict')
        self.assertEqual(self.fake.moves, [])

    def test_initial_signature_must_settle(self):
        self.fake.on_observe = lambda n: setattr(self.fake, 'position', n)
        self.failure('history_view_not_settled')
        self.assertEqual(self.fake.moves, [])

    def test_initial_frame_identity_must_settle(self):
        self.fake.on_observe = lambda n: setattr(self.fake, 'frame', ('view', n))
        self.failure('history_view_not_settled')
        self.assertEqual(self.fake.moves, [])

    def test_initial_row_identity_must_settle_even_if_same_text(self):
        old = self.fake.observe
        def changed(*args, **kwargs):
            view = old(*args, **kwargs)
            view.row_identity = (len(self.fake.observations),)
            return view
        self.fake.observe = changed
        self.failure('history_view_not_settled')
        self.assertEqual(self.fake.moves, [])

    def test_both_directions_four_steps_only_leave_destination(self):
        for direction, delta in (('older', 120), ('newer', -120)):
            with self.subTest(direction=direction):
                self.fake = Fake()
                result = self.run_action(direction=direction, steps=4)
                self.assertEqual(self.fake.moves, [delta] * 4)
                self.assertEqual(len(self.fake.observations), 4)
                self.assertEqual(self.fake.position, 4 if delta > 0 else -4)
                self.assertTrue(self.fake.navigation_started)
                self.assertFalse(self.fake.submission_started)
                self.assertEqual(result['counts'], {'requested_steps': 4, 'completed_steps': 4,
                    'before_rows': 2, 'after_rows': 6, 'viewport_changed': 1})

    def test_success_is_journal_summary_without_message_content(self):
        result = self.run_action()
        self.assertEqual(json.loads(_summary_json(result)), result)
        self.assertEqual(result['status'], 'viewport_observed')
        self.assertEqual(result['verification_level'], 'settled_view_after_bounded_scroll')
        self.assertEqual(result['refs'], {'conversation': REF})
        self.assertEqual(set(self.fake.history_evidence), EVIDENCE_KEYS)
        self.assertTrue(self.fake.history_evidence['viewport_settled'])
        self.assertTrue(self.fake.history_evidence['conversation_preserved'])
        self.assertTrue(self.fake.history_evidence['draft_preserved'])
        self.assertFalse(self.fake.history_evidence['original_viewport_restoration_requested'])
        self.assertTrue(self.fake.history_evidence['not_full_history'])
        self.assertFalse(self.fake.history_evidence['boundary_verified'])
        self.assertNotIn('owned private text', json.dumps([result, self.fake.history_evidence]))

    def test_unchanged_destination_is_success_without_boundary_claim(self):
        self.fake.unchanged = True
        result = self.run_action(steps=4)
        self.assertEqual(result['counts']['viewport_changed'], 0)
        self.assertFalse(self.fake.history_evidence['viewport_changed'])
        self.assertFalse(self.fake.history_evidence['boundary_verified'])
        self.assertEqual(self.fake.moves, [120] * 4)

    def test_guard_failure_before_delivery_has_known_no_navigation(self):
        def guard(phase, delta):
            if phase == 'before':
                raise AdapterError('native_capture_active')
        self.fake.on_wheel = guard
        error = self.failure('native_capture_active')
        self.assertFalse(error.outcome_unknown)
        self.assertFalse(self.fake.history_evidence['delivery_started'])
        self.assertEqual(self.fake.moves, [])

    def test_postdelivery_failure_is_unknown_without_retry_or_inverse(self):
        def failed(phase, delta):
            if phase == 'after':
                error = AdapterError('wheel_result_unknown')
                error.wheel_delivery_started = True
                raise error
        self.fake.on_wheel = failed
        error = self.failure('wheel_result_unknown', steps=4)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual(self.fake.moves, [120])
        self.assertEqual(self.fake.history_evidence['completed_steps'], 0)

    def test_second_step_predelivery_failure_keeps_first_completed_and_unknown(self):
        def failed(phase, delta):
            if phase == 'before' and self.fake.moves:
                raise AdapterError('native_capture_active')
        self.fake.on_wheel = failed
        self.failure('native_capture_active', steps=4)
        self.assertEqual(self.fake.moves, [120])
        self.assertEqual(self.fake.history_evidence['completed_steps'], 1)
        self.assertTrue(self.fake.history_evidence['delivery_started'])

    def test_context_changes_after_delivery_fail_without_inverse(self):
        self.fake.on_wheel = lambda phase, delta: setattr(self.fake, 'context', ('changed',)) if phase == 'after' else None
        self.failure('context_conflict', steps=4)
        self.assertEqual(self.fake.moves, [120])
        self.assertFalse(self.fake.history_evidence['conversation_preserved'])

    def test_draft_changes_after_delivery_fail_without_clearing(self):
        self.fake.on_wheel = lambda phase, delta: setattr(self.fake, 'draft', 'owned draft') if phase == 'after' else None
        self.failure('draft_conflict', steps=4)
        self.assertEqual(self.fake.moves, [120])
        self.assertEqual(self.fake.draft, 'owned draft')
        self.assertFalse(self.fake.history_evidence['draft_preserved'])

    def test_final_context_change_fails_unknown(self):
        self.fake.on_observe = lambda n: setattr(self.fake, 'context', ('changed',)) if n == 3 else None
        self.failure('context_conflict')
        self.assertEqual(self.fake.moves, [120])
        self.assertFalse(self.fake.history_evidence['viewport_settled'])

    def test_final_unstable_view_fails_unknown(self):
        self.fake.on_observe = lambda n: setattr(self.fake, 'position', n) if n >= 3 else None
        self.failure('history_view_not_settled')
        self.assertEqual(self.fake.moves, [120])

    def test_final_unsupported_row_fails_unknown_without_inverse(self):
        def unsupported(n):
            if n == 3:
                raise AdapterError('history_row_unsupported')
        self.fake.on_observe = unsupported
        self.failure('history_row_unsupported')
        self.assertEqual(self.fake.moves, [120])

    def test_expired_or_insufficient_budget_prevents_observation_and_delivery(self):
        for deadline in (0, 2, 3):
            with self.subTest(deadline=deadline):
                self.fake = Fake()
                self.failure('history_budget_exhausted', deadline=deadline)
                self.assertEqual(self.fake.observations, [])
                self.assertEqual(self.fake.moves, [])

    def test_slow_initial_provider_cannot_spend_final_observation_reserve(self):
        self.fake.on_observe = lambda n: setattr(self.fake, 'time', 26) if n == 2 else None
        self.failure('history_budget_exhausted', steps=4)
        self.assertEqual(self.fake.moves, [])

    def test_guard_com_blocking_past_deadline_prevents_native_delivery(self):
        def slow(view):
            if len(self.fake.observations) == 2:
                self.fake.time = 29
        self.fake.on_recheck = slow
        self.failure('history_budget_exhausted')
        self.assertEqual(self.fake.moves, [])

    def test_deadline_after_delivery_is_unknown_and_never_inverses(self):
        self.fake.on_wheel = lambda phase, delta: setattr(self.fake, 'time', 29) if phase == 'after' else None
        self.failure('history_budget_exhausted', steps=4)
        self.assertEqual(self.fake.moves, [120])

    def test_generic_provider_error_does_not_leak_message_or_exception_type(self):
        def bad(_):
            raise RuntimeError('owned private text')
        self.fake.on_observe = bad
        error = self.failure('history_navigation_failed')
        self.assertEqual(str(error), 'history_navigation_failed')
        self.assertNotIn('owned private text', json.dumps(self.fake.history_evidence))

    def test_unknown_adapter_error_code_is_sanitized(self):
        def bad(_):
            raise AdapterError('owned private text', 'also private')
        self.fake.on_observe = bad
        error = self.failure('history_navigation_failed')
        self.assertEqual(str(error), 'history_navigation_failed')

    def test_prior_navigation_unknown_is_not_reset_and_not_retried(self):
        self.fake.navigation_started = True
        self.failure('history_navigation_already_started')
        self.assertEqual(self.fake.observations, [])
        self.assertEqual(self.fake.moves, [])


if __name__ == '__main__':
    unittest.main()
