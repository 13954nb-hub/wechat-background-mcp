"""DPI reader tests: fakes plus own-thread native reads, never a Weixin target."""
import ctypes
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg import monitor


class FakeUser32:
    def __init__(self):
        self.local = threading.local()
        self.calls = []
        self.fail_enter = False
        self.fail_restore = False
        self.wrong_enter = False
        self.wrong_restore = False
        self.query_zero = False

    @property
    def context(self):
        return getattr(self.local, 'context', -1)

    @context.setter
    def context(self, value):
        self.local.context = value

    def SetThreadDpiAwarenessContext(self, context):
        self.calls.append((threading.get_ident(), context))
        previous = self.context
        if context == -4:
            if self.fail_enter:
                return 0
            self.context = -2 if self.wrong_enter else context
        else:
            if self.fail_restore:
                return 0
            self.context = -3 if self.wrong_restore else context
        return previous

    def GetThreadDpiAwarenessContext(self):
        return 0 if self.query_zero else self.context

    def AreDpiAwarenessContextsEqual(self, left, right):
        return bool(left and right and left == right)

    def GetGUIThreadInfo(self, tid, pointer):
        pointer._obj.hwndCapture = 0
        return True


def desktop(cursor=(1421, 791)):
    return {'foreground': 10, 'cursor': list(cursor), 'clipboard_sequence': 3,
            'minimized': True, 'visible_windows': [20], 'capture': 0,
            'cursor_api': 'GetCursorPos', 'cursor_dpi_context': 'per_monitor_v2',
            'cursor_coordinate_space': 'screen_coordinates_under_pm_v2'}


class CursorDpiFakeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(monitor, 'cursor_snapshot', None)),
                        'A fixed-context cursor reader has not been implemented')
        self.native = FakeUser32()
        self.patch_native = patch.object(monitor, 'user32', self.native)
        self.patch_native.start()
        self.addCleanup(self.patch_native.stop)
        self.cursor_reads = 0

        def read_cursor():
            self.cursor_reads += 1
            return (1421, 791) if self.native.context == -4 else (568, 316)
        self.patch_cursor = patch.object(monitor.win32gui, 'GetCursorPos', side_effect=read_cursor)
        self.patch_cursor.start()
        self.addCleanup(self.patch_cursor.stop)

    def failure(self, code):
        with self.assertRaises(monitor.CursorDpiError) as caught:
            monitor.cursor_snapshot()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_enter_failure_has_no_cursor_read_or_fabricated_snapshot(self):
        self.native.fail_enter = True
        self.failure('cursor_dpi_enter_failed')
        self.assertEqual((self.native.context, self.cursor_reads), (-1, 0))
        self.assertEqual([call[1] for call in self.native.calls], [-4])

    def test_missing_dpi_api_fails_closed(self):
        with patch.object(monitor, 'user32', SimpleNamespace()):
            self.failure('cursor_dpi_enter_failed')
        self.assertEqual(self.cursor_reads, 0)

    def test_wrong_enter_context_is_rejected_and_original_restored(self):
        self.native.wrong_enter = True
        self.failure('cursor_dpi_context_unverified')
        self.assertEqual((self.native.context, self.cursor_reads), (-1, 0))
        self.assertEqual([call[1] for call in self.native.calls], [-4, -1])

    def test_zero_context_query_rejects_read_and_records_unverified_restore(self):
        self.native.query_zero = True
        error = self.failure('cursor_dpi_restore_unverified')
        self.assertEqual(error.primary_error_code, 'cursor_dpi_context_unverified')
        self.assertEqual(error.cleanup_error_code, 'cursor_dpi_restore_unverified')
        self.assertEqual(self.cursor_reads, 0)

    def test_cursor_read_error_restores_context_without_private_error_text(self):
        with patch.object(monitor.win32gui, 'GetCursorPos', side_effect=RuntimeError('private desktop detail')):
            error = self.failure('cursor_read_failed')
        self.assertEqual(self.native.context, -1)
        self.assertEqual(error.primary_error_code, 'cursor_read_failed')
        self.assertIsNone(error.cleanup_error_code)
        self.assertNotIn('private', str(error))

    def test_bad_cursor_value_never_becomes_a_snapshot(self):
        for value in (None, (1,), (True, 2), ('1', 2), (1.0, 2), (1, 2, 3)):
            with self.subTest(value=value), patch.object(monitor.win32gui, 'GetCursorPos', return_value=value):
                self.failure('cursor_read_failed')
                self.assertEqual(self.native.context, -1)

    def test_restore_api_failure_overrides_successful_read(self):
        self.native.fail_restore = True
        error = self.failure('cursor_dpi_restore_failed')
        self.assertIsNone(error.primary_error_code)
        self.assertEqual(error.cleanup_error_code, 'cursor_dpi_restore_failed')
        self.assertEqual(self.cursor_reads, 1)

    def test_restore_reported_success_but_wrong_context_is_rejected(self):
        self.native.wrong_restore = True
        error = self.failure('cursor_dpi_restore_unverified')
        self.assertEqual(error.cleanup_error_code, 'cursor_dpi_restore_unverified')

    def test_restore_verification_provider_error_is_distinct_from_setter_failure(self):
        get_context = self.native.GetThreadDpiAwarenessContext
        def read_context():
            if len(self.native.calls) == 2:
                raise RuntimeError('owned restore verification unavailable')
            return get_context()
        with patch.object(self.native, 'GetThreadDpiAwarenessContext', side_effect=read_context):
            error = self.failure('cursor_dpi_restore_unverified')
        self.assertEqual(self.native.context, -1)
        self.assertEqual(error.cleanup_error_code, 'cursor_dpi_restore_unverified')

    def test_restore_setter_exception_cannot_return_a_successful_snapshot(self):
        set_context = self.native.SetThreadDpiAwarenessContext
        def set_or_fail(context):
            if context != -4:
                raise RuntimeError('owned restore setter failed')
            return set_context(context)
        with patch.object(self.native, 'SetThreadDpiAwarenessContext', side_effect=set_or_fail):
            self.failure('cursor_dpi_restore_failed')

    def test_read_failure_and_restore_failure_both_survive(self):
        self.native.fail_restore = True
        with patch.object(monitor.win32gui, 'GetCursorPos', side_effect=RuntimeError('private')):
            error = self.failure('cursor_dpi_restore_failed')
        self.assertEqual((error.primary_error_code, error.cleanup_error_code),
                         ('cursor_read_failed', 'cursor_dpi_restore_failed'))
        self.assertNotIn('private', str(error))

    def test_context_change_during_cursor_read_is_detected(self):
        def changed():
            self.native.context = -2
            return (568, 316)
        with patch.object(monitor.win32gui, 'GetCursorPos', side_effect=changed):
            self.failure('cursor_dpi_context_unverified')
        self.assertEqual(self.native.context, -1)

    def test_success_contains_explicit_space_labels_and_restores_prior_context(self):
        self.native.context = -2
        result = monitor.cursor_snapshot()
        self.assertEqual(result, {key: desktop()[key] for key in
                                 ('cursor', 'cursor_api', 'cursor_dpi_context', 'cursor_coordinate_space')})
        self.assertEqual(self.native.context, -2)
        self.assertEqual([call[1] for call in self.native.calls], [-4, -2])

    def test_two_threads_restore_their_own_contexts_without_changing_parent(self):
        barrier = threading.Barrier(3, timeout=3)
        results, failures = [], []

        def read_at_barrier():
            barrier.wait()
            barrier.wait()
            return (1421, 791)

        def worker(initial):
            try:
                self.native.context = initial
                result = monitor.cursor_snapshot()
                results.append((initial, self.native.context, result))
            except BaseException as exc:
                failures.append(exc)

        with patch.object(monitor.win32gui, 'GetCursorPos', side_effect=read_at_barrier):
            threads = [threading.Thread(target=worker, args=(initial,)) for initial in (-1, -2)]
            for thread in threads:
                thread.start()
            try:
                barrier.wait()
                self.assertEqual(self.native.context, -1)
            finally:
                barrier.wait()
                for thread in threads:
                    thread.join(3)
        self.assertFalse(failures)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(before == after for before, after, _ in results))
        self.assertTrue(all(result['cursor'] == [1421, 791] for _, _, result in results))
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def fake_desktop(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(monitor.win32process, 'GetWindowThreadProcessId', return_value=(7, 123)))
        stack.enter_context(patch.object(monitor.win32gui, 'GetForegroundWindow', return_value=10))
        stack.enter_context(patch.object(monitor.win32gui, 'IsIconic', return_value=True))
        stack.enter_context(patch.object(monitor.win32clipboard, 'GetClipboardSequenceNumber', return_value=3))
        stack.enter_context(patch.object(monitor, 'visible_windows', return_value=[20]))
        return stack

    def test_full_snapshot_preserves_desktop_fields_and_includes_cursor_labels(self):
        with self.fake_desktop():
            self.assertEqual(monitor.snapshot(123, 20), desktop())

    def test_full_snapshot_propagates_dpi_failure_instead_of_returning_old_coordinates(self):
        self.native.fail_enter = True
        with self.fake_desktop(), self.assertRaises(monitor.CursorDpiError):
            monitor.snapshot(123, 20)


class MonitorDpiAggregationTests(unittest.TestCase):
    def aggregate(self, samples=None, after=None, errors=None):
        with patch.object(monitor, 'snapshot', side_effect=[desktop(), after or desktop()]):
            item = monitor.Monitor(123, 20)
            item.thread = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: False)
            item.samples = samples or []
            item.errors = errors or []
            return item.stop()

    def test_same_space_reading_keeps_existing_success_contract_and_labels(self):
        result = self.aggregate(samples=[desktop(), desktop()])
        self.assertTrue(result['background_observation_passed'])
        self.assertFalse(result['cursor_changed'])
        self.assertEqual(result['observations'], 3)
        self.assertEqual(result['before']['cursor_dpi_context'], 'per_monitor_v2')
        self.assertEqual(result['after']['cursor_coordinate_space'], 'screen_coordinates_under_pm_v2')

    def test_actual_cursor_change_is_still_reported_without_changing_existing_pass_policy(self):
        result = self.aggregate(after=desktop((1500, 800)))
        self.assertTrue(result['cursor_changed'])
        self.assertTrue(result['background_observation_passed'])

    def test_collector_retains_dpi_error_code_and_fails_background_result(self):
        self.assertTrue(hasattr(monitor, 'CursorDpiError'), 'DPI errors need safe structured codes')
        with patch.object(monitor, 'snapshot', return_value=desktop()):
            item = monitor.Monitor(123, 20)
        item.stop_event = SimpleNamespace(wait=lambda _: False, set=lambda: None)
        item.thread = SimpleNamespace(join=lambda _: None, is_alive=lambda: False)
        with patch.object(monitor, 'snapshot', side_effect=monitor.CursorDpiError('cursor_dpi_restore_failed')):
            item._observe()
        self.assertEqual(item.errors, ['cursor_dpi_restore_failed'])
        self.assertEqual(item.samples, [])
        with patch.object(monitor, 'snapshot', return_value=desktop()):
            self.assertFalse(item.stop()['background_observation_passed'])

    def test_final_snapshot_failure_propagates_instead_of_claiming_no_effects(self):
        self.assertTrue(hasattr(monitor, 'CursorDpiError'), 'DPI errors need safe structured codes')
        with patch.object(monitor, 'snapshot', return_value=desktop()):
            item = monitor.Monitor(123, 20)
        item.thread = SimpleNamespace(join=lambda _: None, is_alive=lambda: False)
        with patch.object(monitor, 'snapshot', side_effect=monitor.CursorDpiError('cursor_dpi_restore_failed')):
            with self.assertRaises(monitor.CursorDpiError):
                item.stop()


@unittest.skipUnless(os.name == 'nt', 'Native test only reads this Windows process/thread')
class CursorDpiOwnedNativeTests(unittest.TestCase):
    def test_native_reader_restores_each_owned_thread_context(self):
        self.assertTrue(callable(getattr(monitor, 'cursor_snapshot', None)), 'Fixed-context reader missing')
        native = monitor.user32
        parent_context = native.GetThreadDpiAwarenessContext()
        results, errors = [], []

        def own_thread(requested):
            previous = native.SetThreadDpiAwarenessContext(requested)
            if not previous:
                errors.append('owned_test_context_setup_failed')
                return
            try:
                before = native.GetThreadDpiAwarenessContext()
                result = monitor.cursor_snapshot()
                after = native.GetThreadDpiAwarenessContext()
                results.append((bool(native.AreDpiAwarenessContextsEqual(before, after)), result))
            except BaseException as exc:
                errors.append(type(exc).__name__)
            finally:
                if not native.SetThreadDpiAwarenessContext(previous):
                    errors.append('owned_test_context_restore_failed')

        threads = [threading.Thread(target=own_thread, args=(context,)) for context in (-1, -2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(restored for restored, _ in results))
        self.assertTrue(all(result['cursor_dpi_context'] == 'per_monitor_v2' for _, result in results))
        self.assertTrue(native.AreDpiAwarenessContextsEqual(parent_context, native.GetThreadDpiAwarenessContext()))


if __name__ == '__main__':
    unittest.main()
