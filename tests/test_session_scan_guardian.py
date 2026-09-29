"""Fail-closed worker/guardian checks for bounded session navigation."""

from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import types
import unittest
from unittest.mock import patch

from test_gate import FakeBackend
from wxbg import supervisor, worker
from wxbg.worker import _scan_background_valid
from wxbg.session_scan_contract import normalize_scan_evidence, valid_scan_success
from worker_dpi_stub import worker_monitor_module


ARGS = {'title': 'Example Group', 'max_steps': 2}


def evidence(*, opened=False, steps=2):
    return {
        'direction': 'down',
        'requested_steps': 2, 'attempted_steps': steps,
        'delivered_steps': steps, 'delivery_started': steps > 0,
        'activation_started': opened, 'target_found': opened,
        'selected_and_header_verified': opened, 'reached_end': False,
        'primary_error_code': None,
    }


def body(*, opened=False, steps=2):
    return {
        'ok': True,
        'status': 'opened' if opened else 'not_found_in_bounded_scan',
        'title': ARGS['title'] if opened else None,
        'verification_level': ('selected_row_and_header' if opened
                               else 'bounded_viewport_observation'),
        'counts': {'wheel_steps': steps, 'viewports': steps + 1},
        'background_mode': 'minimized',
    }


DESKTOP = {
    'background_observation_passed': True, 'observations': 1,
    'scan_background_observation_passed': True,
    'target_foreground_observed': False,
    'foreground_changed': False, 'clipboard_changed': False,
    'cursor_changed': False, 'target_restored': False, 'capture_observed': False,
    'new_visible_windows': [], 'monitor_errors': [],
}


class ScanContractTests(unittest.TestCase):
    def test_exact_opened_and_not_found(self):
        self.assertTrue(valid_scan_success(body(opened=True), evidence(opened=True),
                                           DESKTOP, ARGS))
        selected = evidence(opened=True)
        selected['activation_started'] = False
        self.assertTrue(valid_scan_success(body(opened=True), selected, DESKTOP, ARGS))
        self.assertTrue(valid_scan_success(body(), evidence(), DESKTOP, ARGS))
        upward = evidence()
        upward['direction'] = 'up'
        self.assertTrue(valid_scan_success(body(), upward, DESKTOP,
                                           {**ARGS, 'direction': 'up'}))
        self.assertFalse(valid_scan_success(body(), upward, DESKTOP, ARGS))
        unchanged_body = body(steps=1)
        unchanged_body['status'] = 'viewport_unchanged'
        unchanged_body['counts'] = {'wheel_steps': 1, 'viewports': 2}
        self.assertTrue(valid_scan_success(unchanged_body, evidence(steps=1),
                                           DESKTOP, ARGS))
        partial_body = body(steps=1)
        partial_body['status'] = 'target_partially_visible'
        partial_body['counts'] = {'wheel_steps': 1, 'viewports': 2}
        partial_evidence = evidence(steps=1)
        partial_evidence['target_found'] = True
        self.assertTrue(valid_scan_success(partial_body, partial_evidence,
                                           DESKTOP, ARGS))
        external_focus = {**DESKTOP, 'foreground_changed': True,
                          'background_observation_passed': False}
        self.assertTrue(valid_scan_success(body(), evidence(), external_focus, ARGS))

    def test_reject_inconsistent_or_private_evidence(self):
        for key, value in (
            ('delivered_steps', 3), ('activation_started', True),
            ('private_chat_text', 'secret'), ('requested_steps', True),
        ):
            with self.subTest(key=key):
                item = evidence()
                item[key] = value
                self.assertFalse(normalize_scan_evidence(item)[1])
        changed = deepcopy(DESKTOP)
        changed['clipboard_changed'] = True
        self.assertFalse(valid_scan_success(body(), evidence(), changed, ARGS))

    def test_worker_allows_external_focus_only_without_input_or_target_focus(self):
        monitor = SimpleNamespace(
            before={'foreground': 101, 'minimized': True},
            samples=[{'foreground': 202, 'minimized': True}],
        )
        report = {**DESKTOP, 'foreground_changed': True,
                  'background_observation_passed': False,
                  'after': {'foreground': 202, 'minimized': True}}
        self.assertTrue(_scan_background_valid(report, monitor, {'hwnd': 999}))
        self.assertTrue(report['scan_background_observation_passed'])
        self.assertFalse(report['target_foreground_observed'])
        report['cursor_changed'] = True
        self.assertFalse(_scan_background_valid(report, monitor, {'hwnd': 999}))
        report['cursor_changed'] = False
        monitor.samples[0]['foreground'] = 999
        self.assertFalse(_scan_background_valid(report, monitor, {'hwnd': 999}))
        self.assertTrue(report['target_foreground_observed'])


class ScanGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.state_dir = self.directory

    def run_guardian(self, reply):
        return supervisor.run_request(
            {'action': 'scan_open_session', 'args': ARGS},
            state_dir=self.directory, backend=self.backend,
            worker_runner=lambda *_: deepcopy(reply),
            mutex_factory=lambda *_: nullcontext(),
        )

    def test_valid_open_and_bounded_miss(self):
        for opened in (False, True):
            with self.subTest(opened=opened):
                reply = {'ok': True, 'result': body(opened=opened),
                         'navigation_evidence': evidence(opened=opened),
                         'evidence': DESKTOP}
                result = self.run_guardian(reply)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['result'], reply['result'])

    def test_scan_only_has_bounded_extended_worker_time(self):
        remaining_seen = []
        reply = {'ok': True, 'result': body(),
                 'navigation_evidence': evidence(), 'evidence': DESKTOP}
        result = supervisor.run_request(
            {'action': 'scan_open_session', 'args': ARGS},
            timeout=supervisor.MAX_SCAN_WORKER_SECONDS,
            state_dir=self.directory, backend=self.backend,
            worker_runner=lambda _, remaining: (
                remaining_seen.append(remaining) or deepcopy(reply)),
            mutex_factory=lambda *_: nullcontext(),
        )
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(remaining_seen), 1)
        self.assertGreater(remaining_seen[0], supervisor.MAX_WORKER_SECONDS)
        self.assertLessEqual(remaining_seen[0], supervisor.MAX_SCAN_WORKER_SECONDS)

    def test_missing_or_mismatched_evidence_never_releases_success(self):
        for change in ('missing', 'bad_count', 'bad_desktop'):
            with self.subTest(change=change):
                reply = {'ok': True, 'result': body(),
                         'navigation_evidence': evidence(),
                         'evidence': deepcopy(DESKTOP)}
                if change == 'missing':
                    del reply['navigation_evidence']
                elif change == 'bad_count':
                    reply['result']['counts']['wheel_steps'] = 1
                else:
                    reply['evidence']['cursor_changed'] = True
                result = self.run_guardian(reply)
                self.assertFalse(result['ok'], result)
                self.assertNotIn('result', result)
                self.assertTrue(result['error']['outcome_unknown'])

    def test_started_failure_is_unknown_and_sanitized(self):
        reply = {'ok': False,
                 'error': {'code': 'private_code', 'detail': 'private text'},
                 'navigation_evidence': evidence(), 'evidence': DESKTOP}
        result = self.run_guardian(reply)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'session_scan_failed')
        self.assertTrue(result['error']['outcome_unknown'])
        self.assertTrue(result['error']['navigation_started'])
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        self.assertLess(result['elapsed_seconds'], 5)
        self.assertNotIn('private text', str(result))

    def test_background_veto_keeps_distinct_error_and_unknown(self):
        reply = {'ok': False,
                 'error': {'code': 'background_side_effect'},
                 'navigation_evidence': evidence(),
                 'evidence': {**DESKTOP, 'foreground_changed': True,
                              'background_observation_passed': False,
                              'target_foreground_observed': True,
                              'scan_background_observation_passed': False}}
        result = self.run_guardian(reply)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'background_side_effect')
        self.assertTrue(result['error']['outcome_unknown'])
        self.assertTrue(result['error']['navigation_started'])
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        self.assertLess(result['elapsed_seconds'], 5)


class ScanWorkerTests(unittest.TestCase):
    def test_scan_receives_supervisor_absolute_deadline(self):
        calls = []

        class FakeAdapter:
            submission_started = False
            navigation_evidence = evidence()

            def __init__(self, target):
                self.target = target

            def dispatch(self, *_):
                raise AssertionError('scan must use deadline-aware collector')

        class FakeMonitor:
            before = {'foreground': 101, 'minimized': True}
            samples = []

            def __init__(self, pid, hwnd):
                del pid, hwnd

            def start(self):
                return self

            def stop(self):
                return {**DESKTOP,
                        'after': {'foreground': 101, 'minimized': True}}

        def collect(adapter, *, title, max_steps, deadline):
            calls.append((adapter.target, title, max_steps, deadline))
            return body()

        adapter_module = types.ModuleType('wxbg.adapter')
        adapter_module.Adapter = FakeAdapter
        monitor_module = worker_monitor_module(FakeMonitor)
        collector_module = types.ModuleType('wxbg.session_navigation')
        collector_module.scan_open_session = collect
        request = {'action': 'scan_open_session', 'args': ARGS,
                   'target': {'pid': 1, 'hwnd': 2}, 'deadline': 456.25}
        output = io.StringIO()
        with patch.dict(sys.modules, {'wxbg.adapter': adapter_module,
                                      'wxbg.monitor': monitor_module,
                                      'wxbg.session_navigation': collector_module}), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                redirect_stdout(output):
            worker.main()
        response = json.loads(output.getvalue())
        self.assertEqual(calls, [(request['target'], ARGS['title'],
                                  ARGS['max_steps'], 456.25)])
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['result'], body())


if __name__ == '__main__':
    unittest.main()
