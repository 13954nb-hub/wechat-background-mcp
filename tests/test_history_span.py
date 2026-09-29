"""Pure fake tests for the work-only history span collector."""
import json
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


SOURCE_ROOT = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SOURCE_ROOT))

import wxbg
from wxbg.policy import AdapterError

try:
    from wxbg import history_span as module
except (ImportError, ModuleNotFoundError):
    module = None


REF = 'a' * 32


class FakeView:
    def __init__(self, fake):
        self.fake = fake
        self.context = fake.context
        self.session_ref = fake.ref
        self.frame_identity = fake.frame
        self.root_rect = fake.root_rect
        self.viewport_rect = fake.viewport_rect
        self.list_node = object()
        self.rows = tuple(fake.rows_for_view())
        self.row_identity = tuple(row.runtime for row in self.rows)
        self.signature = tuple((row.kind, row.text, row.rect) for row in self.rows)

    def recheck(self):
        self.fake.rechecks += 1
        if self.fake.on_recheck:
            self.fake.on_recheck(self)
        if self.fake.context != self.context or self.fake.ref != self.session_ref:
            raise AdapterError('context_conflict')
        if self.fake.frame != self.frame_identity:
            raise AdapterError('history_frame_changed')
        if self.fake.draft:
            raise AdapterError('draft_conflict')


class FakeAdapter:
    def __init__(self):
        self.context = ('session_item_Owned', 'session-runtime', 'field-runtime')
        self.ref = REF
        self.frame = ('message-view', 'list')
        self.root_rect = (0, 0, 3240, 2040)
        self.viewport_rect = (667, 200, 3229, 1680)
        self.draft = ''
        self.position = 0
        self.time = 0.0
        self.observations = []
        self.moves = []
        self.rechecks = 0
        self.on_observe = None
        self.on_recheck = None
        self.on_wheel = None
        self.stay = False
        self.unstable_observation = None
        self.unstable_position = None
        self.fail_at = None
        self.fail_code = 'wheel_result_unknown'
        self.fail_started = True
        self.history_span_started = False

    def rows_for_view(self):
        position = self.position
        third_text = f'card-{position}'
        if (self.unstable_position == position
                and len(self.observations) % 2 == 0):
            third_text += '-changed'
        elif self.unstable_observation == len(self.observations):
            third_text += '-changed'
        return (
            SimpleNamespace(kind='mmui::ChatTextItemView', text='Same text',
                            runtime=f'{position}-first', rect=(667, 105, 3229, 245)),
            SimpleNamespace(kind='mmui::ChatTextItemView', text='Same text',
                            runtime=f'{position}-second', rect=(667, 245, 3229, 347)),
            SimpleNamespace(kind='mmui::ChatBubbleItemView', text=third_text,
                            runtime=f'{position}-third', rect=(667, 347, 3229, 649)),
        )

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
        if adapter is not self:
            raise AssertionError('wrong adapter')
        check_context()
        if self.on_wheel:
            self.on_wheel('before', delta)
        self.moves.append(delta)
        if self.fail_at == len(self.moves):
            error = AdapterError(self.fail_code)
            error.wheel_delivery_started = self.fail_started
            raise error
        if not self.stay:
            self.position += 1 if delta == 120 else -1
        if self.on_wheel:
            self.on_wheel('after', delta)
        try:
            check_context()
        except Exception as error:
            error.wheel_delivery_started = True
            raise

    def sleep(self, seconds):
        self.time += seconds


class HistorySpanTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'history_span implementation is missing')
        self.fake = FakeAdapter()

    def collect(self, *, session_ref=REF, direction='older', steps=1, limit=200, deadline=30.0):
        return module.collect_history_span(
            self.fake, session_ref, direction, steps, limit, deadline,
            observe=self.fake.observe, wheel=self.fake.wheel,
            clock=lambda: self.fake.time, sleep=self.fake.sleep)

    def failure(self, code, *, unknown=None, **kwargs):
        with self.assertRaises(AdapterError) as caught:
            self.collect(**kwargs)
        self.assertEqual(caught.exception.code, code)
        if unknown is not None:
            self.assertEqual(getattr(caught.exception, 'outcome_unknown', None), unknown)
        evidence = json.dumps(self.fake.history_span_evidence, ensure_ascii=False)
        self.assertNotIn('Same text', evidence)
        self.assertNotIn('card-', evidence)
        return caught.exception

    def test_reads_origin_and_each_destination_while_retaining_final_view(self):
        result = self.collect(steps=2)
        self.assertEqual(self.fake.moves, [120, 120])
        self.assertEqual([view['offset'] for view in result['views']], [0, 1, 2])
        self.assertEqual(result['counts']['view_count'], 3)
        self.assertEqual(self.fake.position, 2)
        self.assertTrue(result['final_view_retained'])
        self.assertTrue(result['not_full_history'])
        self.assertFalse(result['boundary_verified'])
        self.assertFalse(result['chronological_order_verified'])

    def test_both_directions_use_one_exact_delta_per_step(self):
        for direction, delta, position in (('older', 120, 4), ('newer', -120, -4)):
            with self.subTest(direction=direction):
                self.fake = FakeAdapter()
                result = self.collect(direction=direction, steps=4)
                self.assertEqual(self.fake.moves, [delta] * 4)
                self.assertEqual(self.fake.position, position)
                self.assertEqual(len(result['views']), 5)

    def test_limit_preserves_kind_text_duplicates_without_runtime_ids(self):
        result = self.collect(limit=200)
        rows = result['views'][0]['rows']
        self.assertEqual([row['kind'] for row in rows],
                         ['mmui::ChatTextItemView', 'mmui::ChatTextItemView',
                          'mmui::ChatBubbleItemView'])
        self.assertEqual([row['text'] for row in rows].count('Same text'), 2)
        self.assertNotIn('runtime', json.dumps(result))

        self.fake = FakeAdapter()
        limited = self.collect(limit=2)
        rows = limited['views'][0]['rows']
        self.assertEqual([row['kind'] for row in rows],
                         ['mmui::ChatTextItemView', 'mmui::ChatBubbleItemView'])
        self.assertEqual([row['text'] for row in rows], ['Same text', 'card-0'])
        self.assertNotIn('runtime', json.dumps(limited))
        self.assertEqual(limited['views'][0]['exposed_count'], 3)
        self.assertEqual(limited['views'][0]['returned_count'], 2)

    def test_equal_viewports_are_kept_as_separate_observations(self):
        self.fake.stay = True
        result = self.collect(steps=2)
        self.assertEqual(len(result['views']), 3)
        self.assertEqual(result['views'][1]['rows'], result['views'][2]['rows'])
        self.assertEqual(self.fake.moves, [120, 120])

    def test_invalid_steps_reject_bool_bounds_float_and_string_before_observation(self):
        for steps in (True, False, 0, 5, -1, 1.0, '1', None):
            with self.subTest(steps=steps):
                self.fake = FakeAdapter()
                self.failure('invalid_history_span_steps', steps=steps)
                self.assertEqual(self.fake.observations, [])
                self.assertEqual(self.fake.moves, [])

    def test_invalid_limit_rejects_bool_bounds_float_and_string_before_observation(self):
        for limit in (True, False, 0, 201, 1.0, '2', None):
            with self.subTest(limit=limit):
                self.fake = FakeAdapter()
                self.failure('invalid_history_span_limit', limit=limit)
                self.assertEqual(self.fake.observations, [])

    def test_invalid_direction_ref_and_deadline_are_rejected(self):
        for kwargs, code in (
            ({'direction': 'Older'}, 'invalid_history_span_direction'),
            ({'direction': None}, 'invalid_history_span_direction'),
            ({'direction': 'older', 'deadline': math.inf}, 'invalid_deadline'),
            ({'direction': 'older', 'deadline': True}, 'invalid_deadline'),
        ):
            with self.subTest(kwargs=kwargs):
                self.fake = FakeAdapter()
                self.failure(code, **kwargs)
        self.fake = FakeAdapter()
        self.failure('invalid_session_ref', session_ref='')

    def test_nonempty_draft_refuses_before_input_and_preserves_text(self):
        self.fake.draft = 'PRIVATE draft'
        self.failure('draft_conflict', unknown=False)
        self.assertEqual(self.fake.draft, 'PRIVATE draft')
        self.assertEqual(self.fake.moves, [])

    def test_transient_view_change_converges_before_input(self):
        self.fake.unstable_observation = 2
        result = self.collect(steps=1)
        self.assertEqual(self.fake.moves, [120])
        self.assertEqual(result['counts']['completed_steps'], 1)

    def test_transient_geometry_observation_retries_without_extra_wheel(self):
        # The first origin sample and first destination sample can each be a
        # half-laid-out Qt row. A pair of later valid equal samples is needed.
        bad_samples = {1, 4}

        def geometry_at_selected_samples(count):
            if count in bad_samples:
                raise AdapterError('history_row_geometry_invalid')

        self.fake.on_observe = geometry_at_selected_samples
        result = self.collect(steps=1)
        self.assertEqual(self.fake.moves, [120])
        self.assertEqual(len(self.fake.observations), 6)
        self.assertEqual(result['counts']['completed_steps'], 1)

    def test_geometry_between_valid_samples_resets_consecutive_pair(self):
        # Sample 2 is invalid, so samples 1 and 3 alone cannot settle a view.
        def fail_second_sample(count):
            if count == 2:
                raise AdapterError('history_row_geometry_invalid')

        self.fake.on_observe = fail_second_sample
        result = self.collect(steps=1)
        self.assertEqual(self.fake.moves, [120])
        self.assertEqual(len(self.fake.observations), 6)
        self.assertEqual(result['counts']['completed_steps'], 1)

    def test_persistent_geometry_stops_with_fixed_code_and_no_extra_wheel(self):
        def fail_every_sample(count):
            raise AdapterError('history_row_geometry_invalid')

        self.fake.on_observe = fail_every_sample
        self.failure('history_row_geometry_invalid', unknown=False)
        self.assertEqual(len(self.fake.observations), 4)
        self.assertEqual(self.fake.moves, [])

        self.fake = FakeAdapter()

        def fail_destination(count):
            if count >= 3:
                raise AdapterError('history_row_geometry_invalid')

        self.fake.on_observe = fail_destination
        self.failure('history_row_geometry_invalid', unknown=True)
        self.assertEqual(len(self.fake.observations), 6)
        self.assertEqual(self.fake.moves, [120])

    def test_context_change_during_initial_pair_returns_no_body(self):
        self.fake.on_observe = lambda count: setattr(self.fake, 'context', ('changed',)) if count == 2 else None
        self.failure('context_conflict', unknown=False)
        self.assertEqual(self.fake.moves, [])

    def test_context_change_after_first_wheel_is_unknown_and_has_no_partial_body(self):
        self.fake.on_wheel = lambda phase, delta: setattr(self.fake, 'context', ('changed',)) if phase == 'after' else None
        error = self.failure('context_conflict', unknown=True)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual(self.fake.moves, [120])

    def test_frame_drift_stops_convergence_immediately_without_retrying_wheel(self):
        self.fake.on_observe = lambda count: setattr(self.fake, 'frame', ('changed',)) if count == 4 else None
        self.failure('history_frame_changed', unknown=True)
        self.assertEqual(self.fake.moves, [120])

    def test_pre_delivery_wheel_failure_is_known_and_not_retried(self):
        self.fake.fail_at = 1
        self.fake.fail_started = False
        self.fake.fail_code = 'native_capture_active'
        self.failure('native_capture_active', unknown=False)
        self.assertEqual(self.fake.moves, [120])

    def test_second_wheel_failure_is_unknown_and_not_retried(self):
        self.fake.fail_at = 2
        self.fake.fail_started = True
        self.failure('wheel_result_unknown', unknown=True, steps=3)
        self.assertEqual(self.fake.moves, [120, 120])

    def test_reverse_view_change_after_prior_wheel_keeps_fixed_code_and_unknown_result(self):
        self.fake.fail_at = 2
        self.fake.fail_started = False
        self.fake.fail_code = 'history_view_changed'
        self.failure('history_view_changed', unknown=True, direction='newer', steps=4)
        self.assertEqual(self.fake.moves, [-120, -120])
        self.assertEqual(self.fake.history_span_evidence['completed_steps'], 1)
        self.assertEqual(self.fake.history_span_evidence['observed_view_count'], 2)
        self.assertEqual(self.fake.history_span_evidence['primary_error_code'],
                         'history_view_changed')

    def test_fixed_history_observation_errors_are_not_hidden_by_span_collector(self):
        for code in ('ambiguous_recipient', 'history_row_identity_invalid',
                     'tree_budget_exceeded',
                     'cannot_disable_auto_focus', 'payment_excluded'):
            with self.subTest(code=code):
                self.fake = FakeAdapter()

                def fail_on_origin(count, *, failure_code=code):
                    if count == 1:
                        raise AdapterError(failure_code)

                self.fake.on_observe = fail_on_origin
                self.failure(code, unknown=False)
                self.assertEqual(self.fake.moves, [])
                self.assertEqual(self.fake.history_span_evidence['primary_error_code'], code)

    def test_unknown_history_error_remains_opaque_after_wheel(self):
        self.fake.fail_at = 1
        self.fake.fail_started = True
        self.fake.fail_code = 'private message body'
        self.failure('history_span_failed', unknown=True)
        self.assertEqual(self.fake.history_span_evidence['primary_error_code'],
                         'history_span_failed')
        self.assertNotIn('private message body',
                         json.dumps(self.fake.history_span_evidence))

    def test_destination_row_change_fails_unknown_without_returning_partial_rows(self):
        self.fake.unstable_position = 1
        self.failure('history_span_view_not_settled', unknown=True)
        self.assertEqual(self.fake.moves, [120])

    def test_persistent_destination_instability_has_a_bounded_wait_and_no_retry_wheel(self):
        self.fake.unstable_position = 1
        self.failure('history_span_view_not_settled', unknown=True, steps=1)
        self.assertEqual(self.fake.moves, [120])

    def test_deadline_reserve_prevents_any_observation_or_input(self):
        self.failure('history_span_budget_exhausted', unknown=False, deadline=2.0, steps=4)
        self.assertEqual(self.fake.observations, [])
        self.assertEqual(self.fake.moves, [])

        self.fake = FakeAdapter()
        self.fake.on_wheel = lambda phase, delta: setattr(self.fake, 'time', 29.0) if phase == 'after' else None
        self.failure('history_span_budget_exhausted', unknown=True, deadline=30.0, steps=4)
        self.assertEqual(self.fake.moves, [120])

    def test_evidence_is_typed_metadata_only_and_success_has_fixed_flags(self):
        result = self.collect(steps=1)
        evidence = self.fake.history_span_evidence
        self.assertEqual(evidence['completed_steps'], 1)
        self.assertEqual(evidence['observed_view_count'], 2)
        self.assertTrue(evidence['final_view_retained'])
        self.assertFalse(evidence['chronological_order_verified'])
        self.assertNotIn('Same text', json.dumps(evidence))
        self.assertNotIn('rows', evidence)
        self.assertEqual(result['refs'], {'conversation': REF})


if __name__ == '__main__':
    unittest.main()
