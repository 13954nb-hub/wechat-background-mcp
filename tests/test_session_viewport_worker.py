"""Worker routes bounded viewport scans through the guarded collector."""
from contextlib import redirect_stdout
import io
import json
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from wxbg import worker
from test_session_viewport_guardian import BODY, DESKTOP, EVIDENCE
from worker_dpi_stub import worker_monitor_module


class ViewportWorkerTests(unittest.TestCase):
    def test_routes_scan_with_absolute_deadline_and_evidence(self):
        calls = []

        class FakeAdapter:
            submission_started = False
            navigation_evidence = EVIDENCE

            def __init__(self, target):
                self.target = target

            def dispatch(self, *_):
                raise AssertionError('viewport scan needs deadline-aware route')

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

        def scan(adapter, *, max_steps, direction, deadline):
            calls.append((adapter.target, max_steps, direction, deadline))
            return BODY

        adapter_module = ModuleType('wxbg.adapter')
        adapter_module.Adapter = FakeAdapter
        monitor_module = worker_monitor_module(FakeMonitor)
        collector_module = ModuleType('wxbg.session_viewport_scan')
        collector_module.scan_session_viewports = scan
        request = {
            'action': 'scan_session_viewports',
            'args': {'max_steps': 1, 'direction': 'down'},
            'target': {'pid': 1, 'hwnd': 2}, 'deadline': 456.25,
        }
        output = io.StringIO()
        with patch.dict(sys.modules, {
                'wxbg.adapter': adapter_module,
                'wxbg.monitor': monitor_module,
                'wxbg.session_viewport_scan': collector_module}), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                redirect_stdout(output):
            worker.main()
        response = json.loads(output.getvalue())
        self.assertTrue(response['ok'], response)
        self.assertEqual(response['result'], BODY)
        self.assertEqual(calls, [(request['target'], 1, 'down', 456.25)])
        self.assertTrue(response['evidence']['scan_background_observation_passed'])


if __name__ == '__main__':
    unittest.main()
