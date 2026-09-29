"""Pure fakes: COM lifecycle and exact interval boundaries, no UI objects."""
import importlib
import threading
import unittest

try:
    module = importlib.import_module('wxbg.uia_events')
except ModuleNotFoundError:
    module = None

TARGET = {'pid': 42, 'hwnd': 99, 'created': 123.0}


class Backend:
    def __init__(self):
        self.calls = []; self.fail_open = False; self.fail_add = None; self.fail_remove = None
        self.zero = threading.Event(); self.zero.set(); self.released = False
        self.startup = False; self.late = False
    def open(self, target, emit):
        self.emit = emit; self.calls.append(('open', threading.get_ident()))
        if self.fail_open: raise RuntimeError('private provider detail')
        return {'owner_tid': threading.get_native_id(), 'owner_mta': True, 'owner_windowless': True,
                'identity_verified': True, 'auto_focus_disabled': True}
    def add(self, kind):
        self.calls.append(('add', kind, threading.get_ident()))
        if self.startup: self.emit('text', 20015)
        if kind == self.fail_add: raise RuntimeError('private add detail')
    def remove(self, kind):
        self.calls.append(('remove', kind, threading.get_ident()))
        if self.late: self.emit('notification', 20035)
        if kind == self.fail_remove: raise RuntimeError('private remove detail')
    def release_handler_refs(self):
        self.calls.append(('release', threading.get_ident())); self.released = True
    def refs_released(self): return self.released and self.zero.is_set()
    def close(self): self.calls.append(('close', threading.get_ident()))


class EventTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'production event module not implemented')
        self.items = []; self.now = 1_000_000_000
    def item(self, backend=None, capacity=128):
        backend = backend or Backend()
        item = module.EventWindow(TARGET, max_events=capacity,
                                  _backend_factory=lambda: backend, _clock_ns=lambda: self.now)
        self.items.append((item, backend)); return item, backend
    def ready(self, backend=None, capacity=128):
        item, backend = self.item(backend, capacity); item.start()
        self.assertTrue(item.wait_ready(1)); return item, backend
    def tearDown(self):
        for item, backend in self.items:
            backend.fail_remove = None; backend.zero.set(); item.stop(1)
    def test_ready_does_not_arm_and_startup_is_excluded(self):
        backend = Backend(); backend.startup = True
        item, backend = self.ready(backend); backend.emit('notification', 20035)
        before = item.snapshot(); self.assertFalse(before['armed'])
        self.assertEqual(before['startup_dropped'], 3); self.assertEqual(before['events'], [])
        item.arm(1); backend.emit('text', 20015)
        result = item.stop(1)
        self.assertEqual(result['armed_counts'], {'notification': 0, 'text': 1})
        self.assertEqual(result['startup_dropped'], 3)
    def test_deadline_is_exclusive_and_late_does_not_change_armed(self):
        item, backend = self.ready(); item.arm(1)
        self.now = 1_999_999_999; backend.emit('text', 20015)
        self.now = 2_000_000_000; backend.emit('notification', 20035)
        self.now = 5_000_000_000; backend.emit('text', 20015)
        result = item.stop(1)
        self.assertEqual(result['armed_counts'], {'notification': 0, 'text': 1})
        self.assertEqual(result['late_dropped'], 2); self.assertEqual(result['closing_ns'], 2_000_000_000)
    def test_late_only_cannot_make_quiet_window_nonempty(self):
        item, backend = self.ready(); item.arm(1); self.now += 1_000_000_001
        backend.emit('text', 20015); result = item.stop(1)
        self.assertEqual(sum(result['armed_counts'].values()), 0)
        self.assertEqual(result['events'], []); self.assertEqual(result['late_dropped'], 1)
    def test_stop_closes_before_remove_callbacks(self):
        item, backend = self.ready(); item.arm(1); backend.emit('text', 20015); backend.late = True
        result = item.stop(1)
        self.assertEqual(result['armed_counts'], {'notification': 0, 'text': 1})
        self.assertEqual(result['late_dropped'], 2)
    def test_queue_128_limit_and_overflow_keeps_armed_counts(self):
        item, backend = self.ready(); item.arm(1)
        for _ in range(140): backend.emit('notification', 20035)
        result = item.stop(1)
        self.assertEqual(len(result['events']), 128); self.assertEqual(result['overflow_dropped'], 12)
        self.assertEqual(result['armed_counts']['notification'], 140)
        self.assertTrue(all(set(x) == {'kind', 'event_id', 'time_ns', 'thread_id'} for x in result['events']))
    def test_owner_add_remove_reverse_same_thread_and_refs_zero(self):
        item, backend = self.ready(); item.arm(1); result = item.stop(1)
        self.assertEqual([x[:2] for x in backend.calls if x[0] in ('add', 'remove')],
                         [('add', 'notification'), ('add', 'text'), ('remove', 'text'), ('remove', 'notification')])
        self.assertEqual(len({x[-1] for x in backend.calls}), 1)
        self.assertNotEqual(backend.calls[0][-1], threading.get_ident())
        self.assertTrue(result['owner_exited']); self.assertTrue(result['handler_refs_released'])
        self.assertFalse(result['cleanup_pending'])
    def test_partial_add_only_removes_successful_prefix(self):
        item, backend = self.item(); backend.fail_add = 'text'; item.start()
        self.assertFalse(item.wait_ready(1)); result = item.stop(1)
        self.assertEqual(result['error_code'], 'subscribe_failed'); self.assertFalse(result['armed'])
        self.assertEqual([x[1] for x in backend.calls if x[0] == 'remove'], ['notification'])
        self.assertTrue(result['owner_exited']); self.assertNotIn('private', repr(result))
    def test_open_failure_closes_without_add(self):
        item, backend = self.item(); backend.fail_open = True; item.start()
        self.assertFalse(item.wait_ready(1)); result = item.stop(1)
        self.assertEqual(result['error_code'], 'open_failed')
        self.assertFalse(any(x[0] == 'add' for x in backend.calls)); self.assertTrue(result['owner_exited'])
    def test_factory_failure_cleans_owner(self):
        def fail(): raise RuntimeError('private constructor detail')
        item = module.EventWindow(TARGET, _backend_factory=fail).start()
        self.assertFalse(item.wait_ready(1)); result = item.stop(1)
        self.assertTrue(result['owner_exited']); self.assertTrue(result['handler_refs_released'])
        self.assertFalse(result['cleanup_pending'])
    def test_remove_failure_retains_objects_and_retries(self):
        item, backend = self.ready(); item.arm(1); backend.fail_remove = 'text'
        result = item.stop(.03)
        self.assertTrue(result['cleanup_pending']); self.assertFalse(backend.released)
        self.assertEqual(result['pending_registrations'], ['text'])
        backend.fail_remove = None; result = item.stop(1)
        self.assertTrue(result['owner_exited']); self.assertTrue(result['registrations_removed'])
        self.assertEqual(len({x[-1] for x in backend.calls}), 1)
    def test_external_refs_hold_owner_until_final_release(self):
        item, backend = self.ready(); item.arm(1); backend.zero.clear()
        result = item.stop(.03)
        self.assertTrue(result['registrations_removed']); self.assertFalse(result['handler_refs_released'])
        self.assertFalse(result['owner_exited']); backend.zero.set()
        self.assertTrue(item.stop(1)['owner_exited'])
    def test_invalid_callback_is_fixed_counter_only(self):
        item, backend = self.ready(); item.arm(1)
        backend.emit('PRIVATE TEXT', 20015); backend.emit('text', 20035)
        result = item.stop(1); self.assertEqual(result['invalid_dropped'], 2)
        self.assertEqual(result['events'], []); self.assertNotIn('PRIVATE', repr(result))
    def test_arm_validation_and_no_rearm(self):
        item, backend = self.item()
        with self.assertRaises(RuntimeError): item.arm(1)
        item.start(); self.assertTrue(item.wait_ready(1))
        for invalid in (True, 0, 61, 1.0, '1'):
            with self.assertRaises(ValueError): item.arm(invalid)
        item.arm(1)
        with self.assertRaises(RuntimeError): item.arm(1)
        item.stop(1)
        with self.assertRaises(RuntimeError): item.arm(1)
    def test_target_and_capacity_validation(self):
        for invalid in (True, 0, 129, 1.0):
            with self.assertRaises(ValueError): module.EventWindow(TARGET, max_events=invalid)
        for invalid in ({}, dict(TARGET, pid=True), dict(TARGET, created=float('nan'))):
            with self.assertRaises(ValueError): module.EventWindow(invalid)
    def test_stop_before_start_and_repeat_no_operations(self):
        item, backend = self.item(); result = item.stop(0)
        self.assertTrue(result['owner_exited']); self.assertEqual(backend.calls, [])
        item.stop(0)
        with self.assertRaises(RuntimeError): item.start()
    def test_auto_focus_readback_required(self):
        class Client:
            def __init__(self, ignore=False, fail=False): self.value = True; self.ignore = ignore; self.fail = fail
            @property
            def AutoSetFocus(self):
                if self.fail: raise RuntimeError('getter failed')
                return self.value
            @AutoSetFocus.setter
            def AutoSetFocus(self, value):
                if not self.ignore: self.value = value
        client = Client(); module._disable_auto_focus(client); self.assertIs(client.value, False)
        with self.assertRaisesRegex(RuntimeError, 'cannot_disable_auto_focus'): module._disable_auto_focus(Client(ignore=True))
        with self.assertRaises(RuntimeError): module._disable_auto_focus(Client(fail=True))


if __name__ == '__main__': unittest.main()
