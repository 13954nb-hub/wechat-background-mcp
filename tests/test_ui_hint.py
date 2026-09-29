"""Bounded UI hint orchestration with virtual time; never touches Windows UI."""
import importlib
import unittest
from wxbg.policy import AdapterError

try:
    module = importlib.import_module('wxbg.ui_hint')
except ModuleNotFoundError:
    module = None

TARGET = {'pid': 42, 'hwnd': 99, 'created': 123.0}


class Clock:
    def __init__(self): self.now = 100.0; self.on_sleep = None
    def __call__(self): return self.now
    def sleep(self, amount):
        self.now += amount
        if self.on_sleep: self.on_sleep()


class Listener:
    def __init__(self, clock):
        self.clock = clock; self.ready_delay = 0; self.ready_ok = True; self.error = None
        self.started = False; self.armed = False; self.end = None; self.closed = None
        self.counts = {'notification': 0, 'text': 0}; self.buffered = 0; self.overflow = 0
        self.late = 0; self.startup = 0; self.invalid = 0; self.stop_calls = []
        self.pending = False; self.stop_raises = False; self.auto_focus = True
    def start(self): self.started = True; return self
    def wait_ready(self, timeout):
        self.clock.now += min(timeout, self.ready_delay)
        return self.ready_ok and self.ready_delay <= timeout
    def arm(self, duration):
        self.armed = True; self.began = self.clock(); self.end = self.began + duration
        return self.snapshot()
    def emit(self, kind='text'):
        if self.closed is not None or (self.end is not None and self.clock() >= self.end): self.late += 1
        elif not self.armed: self.startup += 1
        else:
            self.counts[kind] += 1
            if self.buffered < 128: self.buffered += 1
            else: self.overflow += 1
    def snapshot(self):
        return {'state': 'ready' if self.closed is None else 'stopped', 'ready': self.ready_ok,
                'error_code': self.error, 'armed': self.armed,
                'armed_ns': int(self.began * 1e9) if self.armed else None,
                'window_end_ns': int(self.end * 1e9) if self.end is not None else None,
                'closing_ns': int(self.closed * 1e9) if self.closed is not None else None,
                'armed_counts': dict(self.counts), 'events': [{}] * self.buffered,
                'startup_dropped': self.startup, 'late_dropped': self.late,
                'overflow_dropped': self.overflow, 'invalid_dropped': self.invalid,
                'registrations_removed': self.closed is not None and not self.pending,
                'handler_refs_released': self.closed is not None and not self.pending,
                'owner_exited': self.closed is not None and not self.pending, 'cleanup_pending': self.pending,
                'owner_diagnostics': {'auto_focus_disabled': self.auto_focus, 'owner_mta': True,
                                      'owner_windowless': True, 'identity_verified': True}}
    def stop(self, timeout):
        self.stop_calls.append(timeout)
        if self.stop_raises: raise RuntimeError('PRIVATE stop text')
        if self.closed is None: self.closed = min(self.clock(), self.end) if self.end is not None else self.clock()
        return self.snapshot()


class HintTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'production ui hint module not implemented')
        self.clock = Clock(); self.listener = Listener(self.clock); self.checks = 0; self.created = 0
    def factory(self, target, max_events=128):
        self.created += 1; self.assertEqual(target, TARGET); self.assertEqual(max_events, 128)
        return self.listener
    def background(self): self.checks += 1
    def run_hint(self, seconds=1, deadline=130, check=None):
        return module.wait_for_ui_hint(TARGET, seconds, deadline, listener_factory=self.factory,
                                      clock=self.clock, sleep=self.clock.sleep,
                                      check_background=check or self.background)
    def test_complete_quiet_interval_is_normal_timeout(self):
        response = self.run_hint(); result = response['result']
        self.assertEqual(result['status'], 'timed_out'); self.assertEqual(result['armed_window_ms'], 1000)
        self.assertEqual(result['counts'], dict(notification=0, text_changed=0, buffered=0, overflow_dropped=0, after_close_dropped=0))
        self.assertEqual(result['coverage'], dict(continuous=False, complete=False, recipient_known=False, missed_count=None))
        self.assertEqual(set(response), {'result', 'event_evidence'})
        self.assertGreater(self.checks, 3); self.assertTrue(self.listener.stop_calls)
        self.assertEqual(set(response['event_evidence']), set(module.EVENT_EVIDENCE_KEYS))
    def test_first_hint_returns_early_and_no_extra_action(self):
        self.clock.on_sleep = lambda: self.listener.emit()
        response = self.run_hint(seconds=15); result = response['result']
        self.assertEqual(result['status'], 'hint_observed'); self.assertLess(result['armed_window_ms'], 1000)
        self.assertEqual(result['observed_kinds'], ['text_changed']); self.assertEqual(self.created, 1)
    def test_expired_callback_cannot_change_quiet_to_hint(self):
        def delayed(): self.clock.now = 103; self.listener.emit('notification')
        self.clock.on_sleep = delayed
        response = self.run_hint(); self.assertEqual(response['result']['status'], 'timed_out')
        self.assertEqual(response['result']['counts']['after_close_dropped'], 1)
        self.assertEqual(response['result']['armed_window_ms'], 1000)
    def test_startup_not_counted_as_hint(self):
        original = self.listener.wait_ready
        def ready(timeout): self.listener.emit(); return original(timeout)
        self.listener.wait_ready = ready
        response = self.run_hint(); self.assertEqual(response['result']['status'], 'timed_out')
        self.assertEqual(response['event_evidence']['startup_dropped'], 1)
    def test_strict_timeout_validation_before_listener(self):
        for value in (True, 0, 61, 1.0, '1', None):
            with self.assertRaises(AdapterError) as caught: self.run_hint(seconds=value)
            self.assertEqual(caught.exception.code, 'invalid_timeout')
            self.assertFalse(caught.exception.event_evidence['started'])
        self.assertEqual(self.created, 0)
    def test_invalid_deadline_before_listener(self):
        for value in (True, None, float('inf'), float('nan'), '130'):
            with self.assertRaises(AdapterError) as caught: self.run_hint(deadline=value)
            self.assertEqual(caught.exception.code, 'invalid_deadline')
        self.assertEqual(self.created, 0)
    def test_missing_background_checker_rejected(self):
        with self.assertRaises(AdapterError) as caught:
            module.wait_for_ui_hint(TARGET, 1, 130, listener_factory=self.factory, clock=self.clock, sleep=self.clock.sleep)
        self.assertEqual(caught.exception.code, 'background_checker_required'); self.assertEqual(self.created, 0)
    def test_insufficient_budget_before_start(self):
        with self.assertRaises(AdapterError) as caught: self.run_hint(deadline=106)
        self.assertEqual(caught.exception.code, 'observation_budget_insufficient'); self.assertEqual(self.created, 0)
    def test_ready_cost_leaves_too_little_budget_no_arm(self):
        self.listener.ready_delay = 4
        with self.assertRaises(AdapterError) as caught: self.run_hint(seconds=3, deadline=112)
        self.assertEqual(caught.exception.code, 'observation_budget_insufficient')
        self.assertFalse(self.listener.armed); self.assertTrue(self.listener.stop_calls)
        self.assertTrue(caught.exception.event_evidence['owner_exited'])
    def test_ready_failure_has_fixed_code_and_cleanup(self):
        self.listener.ready_ok = False; self.listener.error = 'subscribe_failed'
        with self.assertRaises(AdapterError) as caught: self.run_hint()
        self.assertEqual(caught.exception.code, 'event_subscribe_failed')
        self.assertTrue(caught.exception.event_evidence['registrations_removed'])
    def test_auto_focus_evidence_required_before_arm(self):
        self.listener.auto_focus = False
        with self.assertRaises(AdapterError) as caught: self.run_hint()
        self.assertEqual(caught.exception.code, 'event_owner_unverified'); self.assertFalse(self.listener.armed)
    def test_background_check_failure_is_sanitized_and_cleaned(self):
        def check():
            self.checks += 1
            if self.checks == 3: raise AdapterError('PRIVATE identifier', 'PRIVATE chat text')
        with self.assertRaises(AdapterError) as caught: self.run_hint(check=check)
        self.assertEqual(caught.exception.code, 'background_precondition_failed')
        self.assertNotIn('PRIVATE', str(caught.exception)); self.assertTrue(self.listener.stop_calls)
    def test_background_is_rechecked_after_stop(self):
        def check():
            self.checks += 1
            if self.listener.closed is not None: raise RuntimeError('PRIVATE context')
        with self.assertRaises(AdapterError) as caught: self.run_hint(check=check)
        self.assertEqual(caught.exception.code, 'background_precondition_failed')
        self.assertTrue(caught.exception.event_evidence['owner_exited'])
    def test_cleanup_pending_is_never_success_even_with_hint(self):
        self.listener.pending = True; self.clock.on_sleep = lambda: self.listener.emit()
        with self.assertRaises(AdapterError) as caught: self.run_hint()
        self.assertEqual(caught.exception.code, 'event_cleanup_pending')
        self.assertTrue(caught.exception.event_evidence['cleanup_pending'])
        self.assertFalse(caught.exception.event_evidence['owner_exited'])
    def test_stop_exception_reports_unknown_cleanup(self):
        self.listener.stop_raises = True
        with self.assertRaises(AdapterError) as caught: self.run_hint()
        self.assertEqual(caught.exception.code, 'event_cleanup_failed')
        self.assertIsNone(caught.exception.event_evidence['owner_exited'])
        self.assertNotIn('PRIVATE', str(caught.exception))
    def test_primary_failure_and_cleanup_failure_both_preserved(self):
        self.listener.ready_ok = False; self.listener.pending = True
        with self.assertRaises(AdapterError) as caught: self.run_hint()
        self.assertEqual(caught.exception.event_evidence['primary_error_code'], 'event_ready_timeout')
        self.assertEqual(caught.exception.event_evidence['cleanup_error_code'], 'event_cleanup_pending')
    def test_overflow_aggregate_and_kinds_are_constant_schema(self):
        def burst():
            self.listener.emit('notification')
            for _ in range(140): self.listener.emit()
        self.clock.on_sleep = burst; response = self.run_hint(); result = response['result']
        self.assertEqual(result['counts'], dict(notification=1, text_changed=140, buffered=128, overflow_dropped=13, after_close_dropped=0))
        self.assertEqual(result['observed_kinds'], ['notification', 'text_changed'])
        self.assertEqual(set(result), {'status','scope','observed_kinds','counts','requested_wait_ms','armed_window_ms','coverage'})
    def test_listener_factory_failure_has_evidence_and_no_raw_detail(self):
        def fail(*args, **kwargs): raise RuntimeError('PRIVATE factory text')
        with self.assertRaises(AdapterError) as caught:
            module.wait_for_ui_hint(TARGET, 1, 130, listener_factory=fail, clock=self.clock, sleep=self.clock.sleep, check_background=self.background)
        self.assertEqual(caught.exception.code, 'event_listener_failed'); self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertFalse(caught.exception.event_evidence['started'])
    def test_foreign_adapter_error_code_is_not_exported(self):
        def fail(*args, **kwargs): raise AdapterError('PRIVATE chat identifier')
        with self.assertRaises(AdapterError) as caught:
            module.wait_for_ui_hint(TARGET, 1, 130, listener_factory=fail, clock=self.clock, sleep=self.clock.sleep, check_background=self.background)
        self.assertEqual(caught.exception.code, 'event_listener_failed')
        self.assertNotIn('PRIVATE', repr(caught.exception.event_evidence))
    def test_invalid_counter_metadata_cannot_succeed(self):
        def malformed(): self.listener.counts['text'] = True
        self.clock.on_sleep = malformed
        with self.assertRaises(AdapterError) as caught: self.run_hint()
        self.assertEqual(caught.exception.code, 'event_listener_failed')
        self.assertTrue(caught.exception.event_evidence['owner_exited'])


if __name__ == '__main__': unittest.main()
