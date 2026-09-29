import contextlib
import ctypes
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest import mock

try:
    from wxbg import gate, supervisor
except ImportError:
    gate = supervisor = None


class FakeBackend:
    def __init__(self, value=0):
        self.value = value
        self.writes = []
        self.alive = True
        self.owner_is_alive = False
        self.identity_changed = False
        self.fail_restore = False
        self.fail_validation = False
        self.closed = False
        self.state_dir = None
        self.target = {"pid": 222, "created": 1234.5, "hwnd": 444}

    def owner_identity(self):
        return {"pid": 10, "created": 1.5, "session": 1, "sid": "S-1-test"}

    def owner_alive(self, owner):
        return self.owner_is_alive

    def discover(self):
        return dict(self.target)

    def target_alive(self, target):
        return self.alive and target == self.target

    def validate(self, target):
        if self.fail_validation:
            raise gate.GateError("UNSUPPORTED_TARGET", "fake validation rejected")
        if not self.target_alive(target):
            raise gate.GateError("TARGET_EXITED", "target gone")
        return {
            **target, "sha256": gate.EXPECTED_SHA, "module_base": 0x100000 if not self.identity_changed else 0x200000,
            "address": (0x100000 if not self.identity_changed else 0x200000) + gate.GATE_RVA,
            "rva": gate.GATE_RVA, "image_size": gate.EXPECTED_IMAGE_SIZE,
            "dll_size": gate.EXPECTED_DLL_SIZE, "session": 1,
        }

    def read_byte(self, identity):
        return self.value

    def write_byte(self, identity, value):
        if value == 1 and self.state_dir:
            prepared = json.loads((self.state_dir / gate.JOURNAL_NAME).read_text())
            if prepared["state"] != "prepared":
                raise RuntimeError("write attempted before prepared journal")
        if value == 0 and self.fail_restore:
            raise OSError("fake restore failure")
        self.value = value
        self.writes.append(value)

    def close(self):
        self.closed = True


class AvailabilityTests(unittest.TestCase):
    def test_implementation_is_available(self):
        self.assertIsNotNone(gate, "gate and supervisor implementation must exist")


@unittest.skipIf(gate is None, "implementation pending")
class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.state_dir = self.directory

    def lease(self):
        return gate.GateLease(self.directory, self.backend, self.backend.discover())

    def test_enable_then_restore_with_durable_prepared_record(self):
        lease = self.lease()
        lease.activate()
        self.assertEqual(self.backend.value, 1)
        self.assertEqual(lease.record["state"], "active")
        cleanup = lease.restore()
        self.assertTrue(cleanup["restored"])
        self.assertEqual(self.backend.writes, [1, 0])
        self.assertEqual(json.loads((self.directory / gate.JOURNAL_NAME).read_text())["state"], "restored")

    def test_original_enabled_byte_is_never_disabled(self):
        self.backend.value = 1
        lease = self.lease()
        lease.activate()
        cleanup = lease.restore()
        self.assertEqual(self.backend.writes, [])
        self.assertTrue(cleanup["restored"])

    def test_non_boolean_original_rejected_without_mutation(self):
        self.backend.value = 3
        with self.assertRaises(gate.GateError):
            self.lease().activate()
        self.assertEqual(self.backend.writes, [])

    def test_byte_conflict_does_not_blindly_restore(self):
        lease = self.lease()
        lease.activate()
        self.backend.value = 4
        cleanup = lease.restore()
        self.assertFalse(cleanup["restored"])
        self.assertEqual(cleanup["status"], "recovery_unknown")
        self.assertEqual(self.backend.writes, [1])

    def test_restore_exception_is_returned_as_cleanup_evidence(self):
        lease = self.lease()
        lease.activate()
        self.backend.fail_restore = True
        cleanup = lease.restore()
        self.assertFalse(cleanup["restored"])
        self.assertIn("fake restore failure", str(cleanup["errors"]))

    def test_changed_module_is_not_written_during_restore(self):
        lease = self.lease()
        lease.activate()
        self.backend.identity_changed = True
        self.assertFalse(lease.restore()["restored"])
        self.assertEqual(self.backend.writes, [1])

    def test_target_exit_is_distinct_from_restored(self):
        lease = self.lease()
        lease.activate()
        self.backend.alive = False
        cleanup = lease.restore()
        self.assertEqual(cleanup["status"], "target_exited")
        self.assertIsNone(cleanup["restored"])

    def test_dead_owner_journal_recovers_same_process(self):
        self.lease().activate()
        recovery = gate.recover_pending(self.directory, self.backend)
        self.assertTrue(recovery["restored"])
        self.assertEqual(self.backend.value, 0)

    def test_schema2_restored_lease_is_accepted_without_another_gate_write(self):
        lease = self.lease()
        lease.activate()
        lease.restore()
        path = self.directory / gate.JOURNAL_NAME
        record = json.loads(path.read_text())
        record['schema'] = 2
        record['profile_id'] = 'verified-profile'
        record['profile_digest'] = 'a' * 64
        record['identity']['profile_id'] = record['profile_id']
        record['identity']['profile_digest'] = record['profile_digest']
        gate.atomic_json(path, record)
        writes_before = list(self.backend.writes)

        recovered = gate.recover_pending(self.directory, self.backend)

        self.assertEqual(recovered, {'status': 'already_settled',
                                     'restored': True})
        self.assertEqual(self.backend.writes, writes_before)

    def test_schema2_active_or_unverified_final_lease_is_rejected(self):
        lease = self.lease()
        lease.activate()
        path = self.directory / gate.JOURNAL_NAME
        active = json.loads(path.read_text())
        active['schema'] = 2
        active['profile_id'] = 'verified-profile'
        active['profile_digest'] = 'a' * 64
        active['identity']['profile_id'] = active['profile_id']
        active['identity']['profile_digest'] = active['profile_digest']
        gate.atomic_json(path, active)
        with self.assertRaises(gate.GateError) as caught:
            gate.recover_pending(self.directory, self.backend)
        self.assertEqual(caught.exception.code, 'JOURNAL_INVALID')
        self.assertEqual(self.backend.writes, [1])

        active['state'] = 'restored'
        active['cleanup'] = {'status': 'restored', 'restored': True,
                             'observed': 1, 'errors': []}
        gate.atomic_json(path, active)
        with self.assertRaises(gate.GateError) as caught:
            gate.recover_pending(self.directory, self.backend)
        self.assertEqual(caught.exception.code, 'JOURNAL_INVALID')
        self.assertEqual(self.backend.writes, [1])

    def test_live_owner_cannot_be_stolen(self):
        self.lease().activate()
        self.backend.owner_is_alive = True
        with self.assertRaises(gate.GateError) as caught:
            gate.recover_pending(self.directory, self.backend)
        self.assertEqual(caught.exception.code, "ACTIVE_OWNER")
        self.assertEqual(self.backend.writes, [1])

    def test_tampered_address_is_not_used_for_recovery(self):
        self.lease().activate()
        path = self.directory / gate.JOURNAL_NAME
        data = json.loads(path.read_text())
        data["identity"]["address"] += 1
        path.write_text(json.dumps(data))
        with self.assertRaises(gate.GateError):
            gate.recover_pending(self.directory, self.backend)
        self.assertEqual(self.backend.writes, [1])

    def test_corrupt_journal_blocks_new_operation(self):
        (self.directory / gate.JOURNAL_NAME).write_text("{")
        with self.assertRaises(gate.GateError):
            gate.recover_pending(self.directory, self.backend)
        self.assertEqual(self.backend.writes, [])

    def test_reused_pid_does_not_recover_new_target(self):
        self.lease().activate()
        self.backend.target = {**self.backend.target, "created": 999.0}
        recovery = gate.recover_pending(self.directory, self.backend)
        self.assertEqual(recovery["status"], "target_exited")
        self.assertEqual(self.backend.writes, [1])


@unittest.skipIf(gate is None, "implementation pending")
class SupervisorTests(unittest.TestCase):
    setUp = LeaseTests.setUp

    def run_guardian(self, runner, request=None):
        return supervisor.run_request(
            request or {"action": "read", "args": {}}, state_dir=self.directory,
            backend=self.backend, worker_runner=runner,
            mutex_factory=lambda backend, timeout: contextlib.nullcontext(),
        )

    def test_worker_receives_validated_target_and_cleanup(self):
        def runner(request, timeout):
            self.assertEqual(request["target"], self.backend.target)
            self.assertEqual(self.backend.value, 1)
            self.assertLessEqual(timeout, 30)
            return {"ok": True, "result": {"count": 2}}
        result = self.run_guardian(runner)
        self.assertTrue(result["ok"])
        self.assertTrue(result["cleanup"]["restored"])
        self.assertEqual(result["result"], {"count": 2})
        self.assertTrue(self.backend.closed)

    def test_worker_timeout_is_unknown_and_gate_is_restored(self):
        calls = []
        def runner(request, timeout):
            calls.append(request)
            raise supervisor.WorkerFailure("TIMEOUT", "timed out")
        result = self.run_guardian(runner)
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        self.assertTrue(result["error"]["outcome_unknown"])
        self.assertTrue(result["cleanup"]["restored"])
        self.assertEqual(len(calls), 1)

    def test_worker_crash_preserves_main_and_cleanup_errors(self):
        def runner(request, timeout):
            self.backend.fail_restore = True
            raise supervisor.WorkerFailure("CRASH", "worker crashed")
        result = self.run_guardian(runner)
        self.assertEqual(result["error"]["code"], "CRASH")
        self.assertFalse(result["cleanup"]["restored"])
        self.assertTrue(result["cleanup"]["errors"])
        self.assertTrue(self.backend.closed)

    def test_cleanup_failure_cannot_leave_success_true(self):
        def runner(request, timeout):
            self.backend.fail_restore = True
            return {"ok": True, "result": {"sent": True}}
        result = self.run_guardian(runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "CLEANUP_FAILED")
        self.assertEqual(result["worker_result"], {"sent": True})

    def test_target_validation_failure_never_runs_worker(self):
        self.backend.fail_validation = True
        result = self.run_guardian(lambda *_: self.fail("worker must not start"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["worker_started"])
        self.assertFalse(result["error"]["outcome_unknown"])
        self.assertEqual(self.backend.writes, [])

    def test_untrusted_target_request_is_replaced(self):
        def runner(request, timeout):
            self.assertEqual(request["target"], self.backend.target)
            return {"ok": True, "result": {}}
        result = self.run_guardian(runner, {"action": "read", "args": {}, "target": {"pid": 1}})
        self.assertTrue(result["ok"])

    def test_navigation_cleanup_evidence_survives_worker_success_and_failure(self):
        for ok in (True, False):
            with self.subTest(ok=ok):
                navigation = {"attempted": True, "restored": ok,
                              "primary_error_code": None if ok else "contacts_read_failed",
                              "cleanup_error_code": None if ok else "context_changed"}
                reply = {"ok": ok, "result": {"contacts": []}, "navigation_evidence": navigation,
                         "error": {"code": "contacts_context_restore_failed", "submission_started": False}}
                result = self.run_guardian(lambda *_: reply)
                self.assertEqual(result["navigation_evidence"], navigation)
                self.assertEqual(result["ok"], ok)
                self.assertTrue(result["cleanup"]["restored"])
                if not ok:
                    self.assertFalse(result["error"]["outcome_unknown"])

    def test_contact_worker_crash_cannot_imply_navigation_was_restored(self):
        def failed(*_):
            raise supervisor.WorkerFailure('TIMEOUT', 'owned worker timeout')
        result = self.run_guardian(failed, {'action': 'list_contacts', 'args': {}})
        self.assertFalse(result['ok'])
        self.assertTrue(result['cleanup']['restored'])
        self.assertIsNone(result['navigation_evidence']['restored'])
        self.assertEqual(result['navigation_evidence']['restoration_status'], 'unknown')

    def test_contact_success_requires_separate_original_context_restoration_proof(self):
        for navigation in (None, {'restored': False}, {'restored': True}):
            with self.subTest(navigation=navigation):
                reply = {'ok': True, 'result': {'contacts': []}}
                if navigation is not None:
                    reply['navigation_evidence'] = navigation
                result = self.run_guardian(lambda *_: reply, {'action': 'list_contacts', 'args': {}})
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['code'], 'CONTACT_RESTORATION_UNVERIFIED')

    def test_mutex_exit_failure_cannot_report_success(self):
        @contextlib.contextmanager
        def broken_mutex(backend, timeout):
            yield
            raise OSError("mutex cleanup failed")
        result = supervisor.run_request(
            {"action": "read", "args": {}}, state_dir=self.directory,
            backend=self.backend, worker_runner=lambda *_: {"ok": True, "result": {"done": True}},
            mutex_factory=broken_mutex,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["cleanup"]["errors"])
        self.assertEqual(result["worker_result"], {"done": True})

    def test_failed_journal_prepare_prevents_gate_write_and_worker(self):
        with mock.patch.object(gate, "atomic_json", side_effect=OSError("disk full")):
            result = self.run_guardian(lambda *_: self.fail("worker must not run"))
        self.assertFalse(result["ok"])
        self.assertFalse(result["worker_started"])
        self.assertEqual(self.backend.writes, [])
        self.assertTrue(self.backend.closed)

    def test_worker_evidence_and_submission_uncertainty_are_preserved(self):
        reply = {"ok": False, "error": {"code": "SEND_FAILED", "detail": "acceptance not observed", "submission_started": True},
                 "evidence": {"samples": 20, "pure_background": False}}
        result = self.run_guardian(lambda *_: reply)
        self.assertEqual(result["evidence"], reply["evidence"])
        self.assertEqual(result["error"]["message"], reply["error"]["detail"])
        self.assertTrue(result["error"]["submission_started"])
        self.assertTrue(result["error"]["outcome_unknown"])

    def test_success_preserves_worker_evidence(self):
        reply = {"ok": True, "result": {}, "evidence": {"samples": 20}}
        result = self.run_guardian(lambda *_: reply)
        self.assertEqual(result["evidence"], reply["evidence"])

    def test_native_failure_evidence_survives_guardian_and_gate_cleanup(self):
        native = {"passed": False, "primary_error": {"code": "native_selection_timeout"},
                  "cleanup_errors": [{"stage": "restore", "code": "native_cleanup_unverified"}]}
        reply = {"ok": False, "error": {"code": "outcome_unknown", "submission_started": True},
                 "native_evidence": native}
        result = self.run_guardian(lambda *_: reply)
        self.assertEqual(result["native_evidence"], native)
        self.assertTrue(result["error"]["outcome_unknown"])
        self.assertTrue(result["cleanup"]["restored"])


@unittest.skipIf(gate is None, "implementation pending")
class PageValidationTests(unittest.TestCase):
    def test_unwritable_or_executable_or_guard_page_is_rejected(self):
        class FakeKernel:
            protection = 0x04
            def VirtualQueryEx(self, handle, address, buffer, length):
                value = buffer._obj
                value.BaseAddress = 0x11000
                value.AllocationBase = 0x10000
                value.RegionSize = 4096
                value.State = 0x1000
                value.Type = 0x1000000
                value.Protect = self.protection
                return ctypes.sizeof(value)
        backend = object.__new__(gate.WinBackend)
        backend.kernel = FakeKernel()
        backend._page(1, 0x10000, 0x11234)
        for protection in (0x01, 0x02, 0x40, 0x104):
            backend.kernel.protection = protection
            with self.subTest(protection=protection), self.assertRaises(gate.GateError):
                backend._page(1, 0x10000, 0x11234)


@unittest.skipIf(gate is None, "implementation pending")
class WorkerLifetimeTests(unittest.TestCase):
    def test_timed_out_worker_is_killed_and_job_closed_before_return(self):
        order = []
        process = mock.Mock()
        process.returncode = None
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 1), ("", "")]
        process.kill.side_effect = lambda: order.append("kill")
        job = mock.Mock()
        job.attach.side_effect = lambda p: order.append("attach")
        job.close.side_effect = lambda: order.append("close")
        with mock.patch.object(supervisor, "_WorkerJob", return_value=job), mock.patch.object(supervisor.subprocess, "Popen", return_value=process):
            with self.assertRaises(supervisor.WorkerFailure) as caught:
                supervisor._run_worker({"action": "read", "args": {}}, 1)
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertEqual(order, ["attach", "kill", "close"])

    def test_job_still_closes_if_direct_kill_fails(self):
        process = mock.Mock()
        process.returncode = None
        process.poll.return_value = None
        process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 1), ("", "")]
        process.kill.side_effect = OSError("direct kill failed")
        job = mock.Mock()
        with mock.patch.object(supervisor, "_WorkerJob", return_value=job), mock.patch.object(supervisor.subprocess, "Popen", return_value=process):
            with self.assertRaises(supervisor.WorkerFailure) as caught:
                supervisor._run_worker({"action": "read", "args": {}}, 1)
        job.close.assert_called_once()
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertTrue(caught.exception.cleanup_errors)


if __name__ == "__main__":
    unittest.main()
