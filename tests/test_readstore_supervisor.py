import contextlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wxbg import gate, supervisor
from test_gate import FakeBackend


READSTORE_ACTIONS = (
    "readstore_read_inbox",
    "readstore_search",
    "readstore_read_new",
    "readstore_get_attachment",
    "readstore_batch_read_new",
)


class ReadstoreSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.state_dir = self.directory

    def run_request(self, action, runner, *, backend=None, mutex_factory=None, timeout=30):
        return supervisor.run_request(
            {"action": action, "args": {}},
            timeout=timeout,
            state_dir=self.directory,
            backend=backend or self.backend,
            worker_runner=runner,
            mutex_factory=mutex_factory or (lambda *_: contextlib.nullcontext()),
        )

    def test_allowlist_is_exact_and_each_action_runs_without_gate_write(self):
        self.assertEqual(supervisor.READSTORE_ACTIONS, frozenset(READSTORE_ACTIONS))
        for action in READSTORE_ACTIONS:
            with self.subTest(action=action):
                backend = FakeBackend()
                backend.state_dir = self.directory
                validations = []
                original_validate = backend.validate

                def validate(target, _original=original_validate):
                    identity = _original(target)
                    validations.append(identity)
                    return identity

                backend.validate = validate
                result = self.run_request(
                    action,
                    lambda request, timeout: {"ok": True, "result": {"action": request["action"]}},
                    backend=backend,
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(backend.writes, [])
                self.assertEqual(len(validations), 2)
                self.assertEqual(result["cleanup"]["status"], "read_only")
                self.assertTrue(result["cleanup"]["restored"])
                self.assertFalse(result["cleanup"]["gate_touched"])
                self.assertEqual(result["target_validation"]["status"], "stable")

    def test_readstore_worker_error_keeps_read_only_cleanup_and_post_validation(self):
        validations = []
        original_validate = self.backend.validate
        self.backend.validate = lambda target: (
            validations.append(True) or original_validate(target)
        )
        result = self.run_request(
            "readstore_search",
            lambda *_: {"ok": False, "error": {"code": "READSTORE_UNAVAILABLE"}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "READSTORE_UNAVAILABLE")
        self.assertEqual(len(validations), 2)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(result["cleanup"]["status"], "read_only")
        self.assertTrue(result["cleanup"]["restored"])

    def test_readstore_worker_timeout_is_unknown_but_still_post_validated(self):
        validations = []
        original_validate = self.backend.validate
        self.backend.validate = lambda target: (
            validations.append(True) or original_validate(target)
        )

        def timed_out(*_):
            raise supervisor.WorkerFailure("TIMEOUT", "worker deadline expired")

        result = self.run_request("readstore_read_new", timed_out)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertTrue(result["error"]["outcome_unknown"])
        self.assertEqual(len(validations), 2)
        self.assertEqual(self.backend.writes, [])
        self.assertTrue(result["cleanup"]["restored"])

    def test_identity_drift_after_worker_withholds_success(self):
        calls = []
        original_validate = self.backend.validate

        def drifting_validate(target):
            identity = original_validate(target)
            calls.append(identity)
            if len(calls) == 2:
                return {**identity, "module_base": identity["module_base"] + 1,
                        "address": identity["address"] + 1}
            return identity

        self.backend.validate = drifting_validate
        result = self.run_request(
            "readstore_read_inbox",
            lambda *_: {"ok": True, "result": {"rows": []}},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TARGET_IDENTITY_CHANGED")
        self.assertTrue(result["error"]["outcome_unknown"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(result["target_validation"]["status"], "changed")

    def test_pending_recovery_failure_prevents_worker_and_gate_activity(self):
        runner = mock.Mock(side_effect=AssertionError("worker must not start"))
        with mock.patch.object(
            supervisor, "recover_pending",
            side_effect=gate.GateError("RECOVERY_FAILED", "pending recovery failed"),
        ):
            result = self.run_request("readstore_search", runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "RECOVERY_FAILED")
        self.assertFalse(result["worker_started"])
        runner.assert_not_called()
        self.assertEqual(self.backend.writes, [])

    def test_mutex_cleanup_failure_cannot_report_readstore_success(self):
        @contextlib.contextmanager
        def broken_mutex(backend, timeout):
            yield
            raise OSError("mutex cleanup failed")

        result = self.run_request(
            "readstore_read_new",
            lambda *_: {"ok": True, "result": {"rows": []}},
            mutex_factory=broken_mutex,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["cleanup"]["errors"])
        self.assertNotIn('worker_result', result)
        self.assertNotIn('result', result)
        self.assertEqual(self.backend.writes, [])

    def test_backend_close_failure_cannot_report_readstore_success(self):
        class CloseFailureBackend(FakeBackend):
            def close(self):
                self.closed = True
                raise OSError("close failed")

        backend = CloseFailureBackend()
        backend.state_dir = self.directory
        result = self.run_request(
            "readstore_search",
            lambda *_: {"ok": True, "result": {"rows": []}},
            backend=backend,
        )
        self.assertFalse(result["ok"])
        self.assertNotIn('worker_result', result)
        self.assertNotIn('result', result)
        self.assertEqual(result["error"]["code"], "CLEANUP_FAILED")
        self.assertTrue(result["cleanup"]["errors"])
        self.assertEqual(backend.writes, [])

    def test_unhashable_action_is_rejected_without_crashing(self):
        result = supervisor.run_request({'action': [], 'args': {}}, backend=self.backend)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'INVALID_REQUEST')

    def test_bypass_like_request_argument_does_not_bypass_gate_for_normal_action(self):
        seen = []

        def runner(request, timeout):
            seen.append(request)
            return {"ok": True, "result": {"done": True}}

        result = supervisor.run_request(
            {"action": "read", "args": {"bypass_gate": True}},
            state_dir=self.directory,
            backend=self.backend,
            worker_runner=runner,
            mutex_factory=lambda *_: contextlib.nullcontext(),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.backend.writes, [1, 0])
        self.assertEqual(seen[0]["args"], {"bypass_gate": True})


if __name__ == "__main__":
    unittest.main()
