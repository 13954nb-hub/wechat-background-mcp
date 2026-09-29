"""Owned-file / fake-transport tests. Never loads the bridge or contacts Weixin."""
import copy
import ctypes
import hashlib
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

from wxbg.native_driver import Data, NativeAttachmentDriver, VerifiedFile, _WinTransport, _RETAINED_TRANSPORTS
from wxbg.policy import AdapterError


TARGET = {'pid': 77, 'tid': 99, 'hwnd': 88, 'created': 123.0}
ATTACHMENT_POINT = (923, 1910)
ATTACHMENT_SIZE = (3235, 1995)


def snapshot(stage='final', **changes):
    result = dict(magic=0x57424134, version=4, size=736, state=2, error=0,
                  pid=77, tid=99, kind=1, module=0x10000, slot=0x20000,
                  original=0x30000, replacement=0x40000, current=0x30000,
                  installed=0, protection_restored=1, matches=1, shows=0,
                  accepted_shows=1, live=0, resident=1, timer_expired=0,
                  restore_attempts=1, cleanup_unresolved=0, active_filter=0,
                  cleanup_exhausted=0, selection_mode=1, grant_revoked=1,
                  lease_deadline=4000)
    if stage == 'inspect':
        result.update(matches=0, accepted_shows=0, resident=0, selection_mode=0,
                      protection_restored=0, restore_attempts=0)
    elif stage == 'armed':
        result.update(installed=1, active_filter=1, current=0x40000,
                      matches=0, accepted_shows=0, grant_revoked=0, restore_attempts=0)
    elif stage == 'observed':
        result.update(installed=1, active_filter=1, current=0x40000,
                      grant_revoked=0, restore_attempts=0)
    result.update(changes)
    return result


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.has_hook = False
        self.local_loaded = False
        self.open_error = None
        self.close_errors = []
        self.module_error = None
        self.remote = True
        self.steps = {1: [snapshot('inspect')], 5: [snapshot('armed')],
                      3: [snapshot('observed')], 4: [snapshot()]}

    def open(self, target, path, expected_sha):
        self.calls.append(('open', dict(target), str(path)))
        self.local_loaded = True
        if self.open_error:
            raise self.open_error
        self.has_hook = True

    def command(self, command, fixture=None, timeout_ms=3000,
                attachment_point=None, attachment_size=None):
        self.calls.append((command, copy.deepcopy(fixture), timeout_ms,
                           attachment_point, attachment_size))
        values = self.steps[command]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        result = dict(value, command=command)
        if command == 5:
            result.update(x=attachment_point[0], y=attachment_point[1],
                          client_width=attachment_size[0], client_height=attachment_size[1])
        return result

    def close(self):
        self.calls.append(('close',))
        removed = 'unhook' not in self.close_errors
        released = removed and 'free' not in self.close_errors
        if removed:
            self.has_hook = False
        if released:
            self.local_loaded = False
        return {'hook_removed': removed, 'local_module_released': released,
                'errors': list(self.close_errors)}

    def module_present(self, target, path):
        self.calls.append(('module_present', str(path)))
        if self.module_error:
            raise self.module_error
        return self.remote


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='owned-native-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.dll = Path(self.temp.name) / 'immutable-owned-test.dll'
        self.dll.write_bytes(b'Not a library; fake transport never loads this owned file.')
        self.sha = hashlib.sha256(self.dll.read_bytes()).hexdigest()
        self.fixture = {'path': str(Path(self.temp.name) / 'owned.txt'), 'name': 'owned.txt',
                        'size': 4, 'sha256': hashlib.sha256(b'test').hexdigest()}
        self.transport = FakeTransport()
        self.clock = Clock()

    def driver(self, **kwargs):
        return NativeAttachmentDriver(TARGET, self.dll, self.sha, _transport=self.transport,
                                      _clock=self.clock, _sleep=self.clock.sleep,
                                      poll_timeout=.12, cleanup_timeout=.15, **kwargs)

    def failure_evidence(self, driver=None):
        with self.assertRaises(AdapterError) as raised:
            (driver or self.driver()).select_file(self.fixture,
                attachment_point=ATTACHMENT_POINT, attachment_size=ATTACHMENT_SIZE)
        self.assertEqual(raised.exception.code, 'outcome_unknown')
        self.assertIsInstance(raised.exception.evidence, dict)
        return raised.exception.evidence

    def test_success_returns_exact_flat_selection_evidence(self):
        report = self.driver().select_file(self.fixture,
            attachment_point=ATTACHMENT_POINT, attachment_size=ATTACHMENT_SIZE)
        for key, value in {'matches': 1, 'accepted_shows': 1, 'shows': 0, 'live': 0,
                           'installed': 0, 'active_filter': 0, 'protection_restored': 1,
                           'cleanup_unresolved': 0, 'cleanup_exhausted': 0}.items():
            self.assertIs(type(report[key]), int)
            self.assertEqual(report[key], value)
        self.assertIs(report['released'], True)
        self.assertIs(report['grant_revoked'], True)
        self.assertIs(report['passed'], True)
        self.assertEqual(report['status'], 'selected')
        self.assertIs(report['remote_receipt_verified'], False)
        self.assertEqual([c[0] for c in self.transport.calls if isinstance(c[0], int)], [1, 5, 3, 4])

    def test_measured_attachment_point_is_forwarded_only_to_arm(self):
        self.driver().select_file(self.fixture, attachment_point=ATTACHMENT_POINT,
                                  attachment_size=ATTACHMENT_SIZE)
        commands = [call for call in self.transport.calls
                    if isinstance(call[0], int)]
        self.assertEqual(commands[1][0], 5)
        self.assertEqual(commands[1][3], ATTACHMENT_POINT)
        self.assertEqual(commands[1][4], ATTACHMENT_SIZE)
        self.assertTrue(all(call[3] is None and call[4] is None for call in
                            (commands[0], *commands[2:])))

    def test_unverified_attachment_point_stops_before_native_open(self):
        with self.assertRaisesRegex(AdapterError, "invalid_attachment_layout"):
            self.driver().select_file(self.fixture,
                                      attachment_point=(922, 20),
                                      attachment_size=ATTACHMENT_SIZE)
        self.assertEqual(self.transport.calls, [])

    def test_one_instance_never_arms_twice(self):
        driver = self.driver()
        driver.select_file(self.fixture, attachment_point=ATTACHMENT_POINT,
                           attachment_size=ATTACHMENT_SIZE)
        self.failure_evidence(driver)
        self.assertEqual(sum(c[0] == 5 for c in self.transport.calls), 1)

    def test_arm_timeout_still_restores_and_keeps_primary(self):
        self.transport.steps[5] = [TimeoutError('arm transport timeout')]
        evidence = self.failure_evidence()
        self.assertIn('arm transport timeout', str(evidence['primary_error']))
        self.assertTrue(any(c[0] == 4 for c in self.transport.calls))
        self.assertEqual(sum(c[0] == 5 for c in self.transport.calls), 1)
        self.assertIs(evidence['hook_removed'], True)

    def test_primary_and_restore_errors_both_survive(self):
        self.transport.steps[3] = [TimeoutError('poll failed')]
        self.transport.steps[4] = [TimeoutError('restore failed')]
        evidence = self.failure_evidence()
        self.assertIn('poll failed', str(evidence['primary_error']))
        self.assertIn('restore failed', str(evidence['cleanup_errors']))
        self.assertLessEqual(sum(c[0] == 4 for c in self.transport.calls), 3)
        self.assertTrue(any(c[0] == 'close' for c in self.transport.calls))

    def test_busy_inspect_does_not_restore_someone_elses_lease(self):
        self.transport.steps[1] = [snapshot('inspect', error=170)]
        self.failure_evidence()
        self.assertFalse(any(c[0] in (4, 5) for c in self.transport.calls))
        self.assertTrue(any(c[0] == 'close' for c in self.transport.calls))

    def test_zero_addresses_and_wrong_protocol_refused_before_arm(self):
        for changed in ({'current': 0, 'original': 0}, {'version': 2}, {'size': 0},
                        {'pid': 1234}, {'tid': 1234}, {'state': 1}, {'magic': 0}):
            with self.subTest(changed=changed):
                self.transport = FakeTransport()
                self.transport.steps[1] = [snapshot('inspect', **changed)]
                self.failure_evidence()
                self.assertFalse(any(c[0] == 5 for c in self.transport.calls))

    def test_arm_rejected_or_address_changed_is_not_retried(self):
        for changed in ({'error': 5}, {'original': 123}, {'resident': 0}, {'active_filter': 0}):
            with self.subTest(changed=changed):
                self.transport = FakeTransport()
                self.transport.steps[5] = [snapshot('armed', **changed)]
                self.failure_evidence()
                self.assertEqual(sum(c[0] == 5 for c in self.transport.calls), 1)
                self.assertTrue(any(c[0] == 4 for c in self.transport.calls))

    def test_duplicate_or_cancelled_picker_never_passes(self):
        for changed in ({'matches': 2}, {'accepted_shows': 2}, {'shows': 1}, {'error': 170}):
            with self.subTest(changed=changed):
                self.transport = FakeTransport()
                self.transport.steps[3] = [snapshot('observed', **changed)]
                self.failure_evidence()

    def test_poll_timeout_has_no_second_arm(self):
        self.transport.steps[3] = [snapshot('armed')]
        self.failure_evidence()
        self.assertEqual(sum(c[0] == 5 for c in self.transport.calls), 1)
        self.assertLess(self.clock.now, 1)

    def test_every_final_cleanup_invariant_is_required(self):
        for changed in ({'installed': 1}, {'protection_restored': 0}, {'current': 0x44444},
                        {'live': 1}, {'grant_revoked': 0}, {'active_filter': 1},
                        {'cleanup_unresolved': 1}, {'cleanup_exhausted': 1},
                        {'matches': 2}, {'accepted_shows': 0}, {'shows': 1}, {'resident': 0}):
            with self.subTest(changed=changed):
                self.transport = FakeTransport()
                self.transport.steps[4] = [snapshot(**changed)]
                self.transport.steps[3] = [snapshot('observed'), snapshot(**changed)]
                self.failure_evidence()

    def test_unhook_failure_retains_local_library(self):
        self.transport.close_errors = ['unhook']
        evidence = self.failure_evidence()
        self.assertTrue(self.transport.local_loaded)
        self.assertIs(evidence['local_module_released'], False)
        self.assertIn('unhook', str(evidence['cleanup_errors']))

    def test_resident_module_missing_or_inspection_failed_is_failure(self):
        self.transport.remote = False
        self.failure_evidence()
        self.transport = FakeTransport()
        self.transport.module_error = OSError('module enumeration failed')
        evidence = self.failure_evidence()
        self.assertIn('module enumeration failed', str(evidence['cleanup_errors']))

    def test_partial_open_failure_still_releases_local_resources(self):
        self.transport.open_error = OSError('hook installation failed')
        self.failure_evidence()
        self.assertFalse(self.transport.local_loaded)
        self.assertTrue(any(c[0] == 'close' for c in self.transport.calls))

    def test_dll_hash_change_and_bad_descriptor_never_attach(self):
        self.dll.write_bytes(b'changed after immutable manifest')
        self.failure_evidence()
        self.assertFalse(self.transport.calls)
        self.dll.write_bytes(b'Not a library; fake transport never loads this owned file.')
        self.fixture['path'] = r'\\server\share\private.txt'
        self.failure_evidence()
        self.assertFalse(self.transport.calls)


class VerifiedFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='owned-grant-test-', dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'owned.txt'
        self.path.write_bytes(b'owned contents\r\n')
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_descriptor_is_hashed_from_handle_and_read_lock_released(self):
        import win32con, win32file
        held = VerifiedFile(self.path, expected_sha256=self.digest)
        with held as fixture:
            self.assertEqual(fixture['name'], self.path.name)
            self.assertEqual(fixture['sha256'], self.digest)
            self.assertEqual(fixture['size'], len(b'owned contents\r\n'))
            with self.assertRaises(Exception):
                win32file.CreateFile(str(self.path), win32con.GENERIC_WRITE,
                                    win32con.FILE_SHARE_READ, None, win32con.OPEN_EXISTING, 0, None)
            reader = win32file.CreateFile(str(self.path), win32con.GENERIC_READ,
                                         win32con.FILE_SHARE_READ, None, win32con.OPEN_EXISTING, 0, None)
            reader.Close()
        self.path.write_bytes(b'write possible after verified holder exit')
        self.assertTrue(held.closed)

    def test_changed_hash_size_and_oversize_are_rejected(self):
        for kwargs in ({'expected_sha256': '0' * 64}, {'expected_size': 999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(AdapterError):
                with VerifiedFile(self.path, **kwargs):
                    self.fail('invalid file was granted')
        self.path.write_bytes(b'x' * (1024 * 1024 + 1))
        with self.assertRaises(AdapterError):
            with VerifiedFile(self.path):
                self.fail('oversize file was granted')
        self.path.write_bytes(b'cleanup did not leak a locked file')

    def test_invalid_path_forms_rejected_without_opening(self):
        for path in ('relative.txt', r'\\server\share\file.txt', r'\\?\C:\file.txt',
                     'C:\\file.txt:stream', 'C:\\file?.txt', 'C:\\bad\nname.txt'):
            with self.subTest(path=path), self.assertRaises(AdapterError):
                with VerifiedFile(path):
                    self.fail('invalid path was granted')

    def test_directory_and_exception_scope_do_not_leak_handles(self):
        with self.assertRaises(AdapterError):
            with VerifiedFile(self.temp.name):
                self.fail('directory was granted')
        with self.assertRaisesRegex(RuntimeError, 'owned failure'):
            with VerifiedFile(self.path):
                raise RuntimeError('owned failure')
        self.path.write_bytes(b'write succeeds')


class TransportLifecycleTests(unittest.TestCase):
    def transport(self, unhook=True, free=True):
        calls = []
        def invoke(name, result):
            def call(handle):
                calls.append((name, handle))
                if isinstance(result, Exception):
                    raise result
                return result
            return call
        transport = _WinTransport()
        transport.hook = 123
        transport.library = SimpleNamespace(_handle=456)
        transport._u = SimpleNamespace(UnhookWindowsHookEx=invoke('unhook', unhook))
        transport._k = SimpleNamespace(FreeLibrary=invoke('free', free))
        self.addCleanup(lambda: _RETAINED_TRANSPORTS.remove(transport)
                        if transport in _RETAINED_TRANSPORTS else None)
        return transport, calls

    def test_v4_native_layout(self):
        self.assertEqual(ctypes.sizeof(Data), 736)
        for name, offset in {'client_width': 52, 'state': 56, 'error': 60, 'accepted_shows': 112,
                             'grant_revoked': 120, 'module': 128, 'lease_deadline': 168,
                             'client_height': 124, 'expected_size': 176,
                             'fixture_path': 184, 'fixture_sha256': 704}.items():
            self.assertEqual(getattr(Data, name).offset, offset)

    def test_local_release_happens_only_after_successful_unhook(self):
        transport, calls = self.transport()
        self.assertEqual(transport.close(), {'hook_removed': True, 'local_module_released': True, 'errors': []})
        self.assertEqual(calls, [('unhook', 123), ('free', 456)])

    def test_unhook_failure_or_exception_retains_module_without_free(self):
        for fault in (False, OSError('owned unhook failure')):
            with self.subTest(fault=fault):
                transport, calls = self.transport(unhook=fault)
                report = transport.close()
                self.assertFalse(report['hook_removed'])
                self.assertFalse(report['local_module_released'])
                self.assertTrue(report['errors'])
                self.assertEqual(calls, [('unhook', 123)])
                self.assertIn(transport, _RETAINED_TRANSPORTS)

    def test_local_free_failure_retains_module_and_reports_failure(self):
        transport, calls = self.transport(free=False)
        report = transport.close()
        self.assertTrue(report['hook_removed'])
        self.assertFalse(report['local_module_released'])
        self.assertTrue(report['errors'])
        self.assertEqual(calls, [('unhook', 123), ('free', 456)])
        self.assertIn(transport, _RETAINED_TRANSPORTS)


if __name__ == '__main__':
    unittest.main()
