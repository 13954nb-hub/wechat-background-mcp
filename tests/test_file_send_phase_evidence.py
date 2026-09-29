"""Synthetic file-send worker diagnostics; no WeChat or native file selection."""

from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg import supervisor, worker
from test_gate import FakeBackend
from worker_dpi_stub import worker_monitor_module


PHASE = {
    'native_selection_started': True,
    'native_selection_completed': True,
    'embedded_file_staged': True,
    'send_click_attempted': True,
    'local_card_observed': True,
    'private_filename': 'never expose fixture.txt',
}


class FileSendPhaseEvidenceTests(unittest.TestCase):
    def worker_reply(self, *, monitor_stop_fails=False):
        class FakeAdapter:
            submission_started = False

            def __init__(self, _target):
                self.file_send_phase = None

            def dispatch(self, action, _args):
                self.submission_started = True
                self.file_send_phase = dict(PHASE)
                return {'ok': True, 'status': 'submitted'}

        class FakeMonitor:
            def __init__(self, _pid, _hwnd):
                pass

            def start(self):
                return self

            def stop(self):
                if monitor_stop_fails:
                    raise RuntimeError('private monitor detail')
                return {'background_observation_passed': False,
                        'foreground_changed': True}

        request = {'action': 'send_file', 'args': {},
                   'target': {'pid': 1, 'created': 1.0, 'hwnd': 2},
                   'deadline': 100.0}
        output = io.StringIO()
        with patch.dict(sys.modules, {
            'wxbg.adapter': SimpleNamespace(Adapter=FakeAdapter),
            'wxbg.monitor': worker_monitor_module(FakeMonitor),
        }), patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                redirect_stdout(output):
            worker.main()
        return json.loads(output.getvalue())

    def assert_sanitized_phase(self, response):
        expected = {'schema': 'file_send_phase_v1'}
        expected.update({key: True for key in PHASE if key != 'private_filename'})
        self.assertEqual(response.get('evidence', {}).get('file_send_phase'), expected)
        self.assertNotIn('never expose', json.dumps(response))

    def test_monitor_side_effect_keeps_sanitized_phase_and_rejects_success(self):
        reply = self.worker_reply()
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['error']['code'], 'background_side_effect')
        self.assertTrue(reply['error']['submission_started'])
        self.assert_sanitized_phase(reply)

        with tempfile.TemporaryDirectory() as directory:
            result = supervisor.run_request(
                {'action': 'send_file', 'args': {}}, state_dir=Path(directory),
                backend=FakeBackend(), worker_runner=lambda *_: reply,
                mutex_factory=lambda *_: nullcontext(),
            )
        self.assertFalse(result['ok'])
        self.assertNotIn('result', result)
        self.assert_sanitized_phase(result)

    def test_monitor_stop_exception_keeps_sanitized_phase_without_success(self):
        reply = self.worker_reply(monitor_stop_fails=True)
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['error']['code'], 'monitor_failed')
        self.assertTrue(reply['error']['submission_started'])
        self.assert_sanitized_phase(reply)
        self.assertNotIn('private monitor detail', json.dumps(reply))


if __name__ == '__main__':
    unittest.main()
