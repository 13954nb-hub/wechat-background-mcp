"""Pure phase-recorder integration tests; no desktop or Weixin calls."""

from contextlib import redirect_stdout
import io
import json
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg import monitor, phase_recorder, worker
from wxbg.policy import AdapterError
from worker_dpi_stub import worker_monitor_module


def load_phase_recorder():
    return phase_recorder


def desktop(*, foreground=10, cursor=(1, 2), clipboard=3, minimized=True,
            windows=(20,), capture=0):
    return {
        "foreground": foreground,
        "cursor": list(cursor),
        "clipboard_sequence": clipboard,
        "minimized": minimized,
        "visible_windows": list(windows),
        "capture": capture,
    }


def contact_evidence(delivery_started=True):
    return {
        "mode": "bounded_contacts_span",
        "origin": "observed_top",
        "requested_steps": 1,
        "delivery_started": delivery_started,
        "entered": delivery_started,
        "attempted_down_steps": 1 if delivery_started else 0,
        "completed_steps": 1 if delivery_started else 0,
        "observed_view_count": 2 if delivery_started else 1,
        "attempted_restore_steps": 1 if delivery_started else 0,
        "viewport_settled": delivery_started,
        "contacts_view_restored": delivery_started,
        "conversation_restored": True,
        "draft_preserved": True,
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "primary_error_code": None,
        "cleanup_error_code": None,
    }


def phase_body():
    return {
        "ok": True,
        "status": "contact_span_observed",
        "verification_level": "settled_visible_span_with_restoration",
        "background_mode": "minimized",
        "origin": "observed_top",
        "views": [],
        "counts": {
            "requested_steps": 1,
            "completed_steps": 1,
            "view_count": 1,
            "changed_view_count": 0,
        },
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "original_contacts_view_restored": True,
        "original_conversation_restored": True,
    }


class PollPhases:
    def __init__(self, recorder, phases):
        self.recorder = recorder
        self.phases = iter(phases)

    def wait(self, _timeout):
        try:
            self.recorder.set_phase(next(self.phases))
        except StopIteration:
            return True
        return False

    def set(self):
        return None


class FakeThread:
    def __init__(self, alive=False):
        self.alive = alive
        self.join_calls = []

    def join(self, timeout):
        self.join_calls.append(timeout)

    def is_alive(self):
        return self.alive


class MonitorPhaseTests(unittest.TestCase):
    def setUp(self):
        self.recorder_module = load_phase_recorder()

    def make_monitor(self, recorder=None, before=None):
        with patch.object(monitor, "snapshot", return_value=before or desktop()):
            return monitor.Monitor(123, 20, phase_recorder=recorder)

    def test_baseline_success_samples_and_after_are_recorded_by_phase(self):
        recorder = self.recorder_module.PhaseRecorder()
        before = desktop()
        sample_one = desktop(foreground=11, cursor=(2, 2), clipboard=4,
                             windows=(20, 30), capture=20)
        sample_two = desktop(minimized=False)
        item = self.make_monitor(recorder, before)
        item.stop_event = PollPhases(recorder, ("enter_contacts", "restore_chat"))
        item.thread = FakeThread()
        with patch.object(monitor, "snapshot", side_effect=(sample_one, sample_two)):
            item._observe()
        recorder.set_phase("complete")
        with patch.object(monitor, "snapshot", return_value=before):
            result = item.stop()

        summary = result["phase_summary"]
        self.assertEqual(summary["sample_count"], 4)
        self.assertEqual(summary["phase_sample_counts"]["operation"], 1)
        self.assertEqual(summary["phase_sample_counts"]["enter_contacts"], 1)
        self.assertEqual(summary["phase_sample_counts"]["restore_chat"], 1)
        self.assertEqual(summary["phase_sample_counts"]["complete"], 1)
        self.assertEqual(summary["flags"]["foreground_changed"]["first_sample_index"], 1)
        self.assertEqual(summary["flags"]["foreground_changed"]["phase_at_first_hit"],
                         "enter_contacts")
        self.assertEqual(summary["flags"]["target_restored"]["first_sample_index"], 2)
        self.assertEqual(summary["flags"]["target_restored"]["phase_at_first_hit"],
                         "restore_chat")
        self.assertTrue(result["capture_observed"])
        self.assertFalse(result["background_observation_passed"])
        self.assertNotIn("visible_windows", json.dumps(summary))
        self.assertNotIn("private", json.dumps(summary))

    def test_failed_observer_sample_records_monitor_error_without_private_values(self):
        recorder = self.recorder_module.PhaseRecorder()
        item = self.make_monitor(recorder)
        recorder.set_phase("span_read")
        item.stop_event = SimpleNamespace(wait=lambda _timeout: False, set=lambda: None)
        item.thread = FakeThread()
        with patch.object(monitor, "snapshot",
                          side_effect=monitor.CursorDpiError("cursor_read_failed")):
            item._observe()
        with patch.object(monitor, "snapshot", return_value=desktop()):
            result = item.stop()

        metadata = result["phase_summary"]
        self.assertEqual(metadata["flags"]["monitor_error"], {
            "first_sample_index": 1,
            "hit_count": 1,
            "phase_at_first_hit": "span_read",
        })
        self.assertEqual(result["monitor_errors"], ["cursor_read_failed"])
        self.assertFalse(result["background_observation_passed"])
        self.assertNotIn("private", json.dumps(result))

    def test_recorder_failure_cannot_return_background_pass(self):
        class FailingRecorder:
            def observe(self, _flags):
                raise RuntimeError("private recorder detail")

            def snapshot(self):
                return {"schema": "phase_recorder_v1", "phase": "operation",
                        "sample_count": 0, "overflow": False,
                        "phase_sample_counts": {}, "flags": {}}

        item = self.make_monitor(FailingRecorder())
        item.thread = FakeThread()
        with patch.object(monitor, "snapshot", return_value=desktop()):
            result = item.stop()
        self.assertFalse(result["background_observation_passed"])
        self.assertIn("phase_recorder_observe_failed", result["monitor_errors"])
        self.assertNotIn("private recorder detail", json.dumps(result))

    def test_recorder_snapshot_failure_is_explicit(self):
        class SnapshotFailRecorder:
            def observe(self, _flags):
                return None

            def snapshot(self):
                raise RuntimeError("private snapshot detail")

        item = self.make_monitor(SnapshotFailRecorder())
        item.thread = FakeThread()
        with patch.object(monitor, "snapshot", return_value=desktop()), \
                self.assertRaises(monitor.MonitorError) as caught:
            item.stop()
        self.assertEqual(caught.exception.code, "phase_recorder_snapshot_failed")
        self.assertNotIn("private snapshot detail", str(caught.exception))

    def test_after_snapshot_failure_records_monitor_error_before_raising(self):
        recorder = self.recorder_module.PhaseRecorder()
        item = self.make_monitor(recorder)
        item.thread = FakeThread()
        with patch.object(monitor, "snapshot",
                          side_effect=monitor.CursorDpiError("cursor_read_failed")), \
                self.assertRaises(monitor.CursorDpiError):
            item.stop()
        summary = recorder.snapshot()
        self.assertEqual(summary["flags"]["monitor_error"]["first_sample_index"], 1)
        self.assertEqual(summary["flags"]["monitor_error"]["hit_count"], 1)
        self.assertFalse("private" in json.dumps(summary))

    def test_join_timeout_is_explicit_failure(self):
        item = self.make_monitor()
        item.thread = FakeThread(alive=True)
        with self.assertRaises(monitor.MonitorError) as caught:
            item.stop()
        self.assertEqual(caught.exception.code, "monitor_join_timeout")

    def test_no_recorder_preserves_old_evidence_and_has_no_phase_summary(self):
        item = self.make_monitor()
        item.thread = FakeThread()
        with patch.object(monitor, "snapshot", return_value=desktop()):
            result = item.stop()
        self.assertNotIn("phase_summary", result)
        self.assertTrue(result["background_observation_passed"])
        self.assertFalse(result["cursor_changed"])

    def test_real_monitor_thread_completes_without_native_io(self):
        recorder = self.recorder_module.PhaseRecorder()
        before = desktop()
        with patch.object(monitor, "snapshot", return_value=before):
            item = monitor.Monitor(123, 20, phase_recorder=recorder).start()
            time.sleep(0.03)
            result = item.stop()
        self.assertFalse(item.thread.is_alive())
        self.assertGreaterEqual(result["phase_summary"]["sample_count"], 2)
        self.assertEqual(result["phase_summary"]["sample_count"], result["observations"] + 1)
        self.assertTrue(result["background_observation_passed"])


class WorkerPhaseTests(unittest.TestCase):
    def test_image_worker_records_first_foreground_hit_and_still_vetoes(self):
        captured = {}

        class FakeAdapter:
            submission_started = False

            def __init__(self, target):
                self.target = target

        class FakeMonitor:
            def __init__(self, pid, hwnd, phase_recorder=None):
                captured["recorder"] = phase_recorder

            def start(self):
                return self

            def stop(self):
                return {"background_observation_passed": False,
                        "foreground_changed": True,
                        "phase_summary": captured["recorder"].snapshot()}

        def image_run(adapter, *, session_ref, file, deadline):
            adapter.image_phase("image_native_select")
            captured["recorder"].observe({
                name: name == "foreground_changed" for name in phase_recorder.FLAG_NAMES})
            adapter.submission_started = True
            adapter.image_evidence = {"version": 1, "baseline": None,
                                      "selection_frames": [], "final_frames": []}
            return {"status": "local_image_transition_observed"}

        request = {"action": "send_image", "args": {"session_ref": "a" * 32,
                   "file": {}}, "target": {"pid": 1, "hwnd": 2}, "deadline": 123.0}
        output = io.StringIO()
        with patch.dict(sys.modules, {
            "wxbg.adapter": SimpleNamespace(Adapter=FakeAdapter),
            "wxbg.monitor": worker_monitor_module(FakeMonitor),
            "wxbg.image_actions": SimpleNamespace(run=image_run),
        }), patch.object(sys, "stdin", io.StringIO(json.dumps(request))), \
                redirect_stdout(output):
            worker.main()
        response = json.loads(output.getvalue())
        self.assertIsNotNone(captured["recorder"])
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "background_side_effect")
        self.assertTrue(response["error"]["submission_started"])
        self.assertEqual(response["evidence"]["phase_summary"]["flags"]
                         ["foreground_changed"]["phase_at_first_hit"],
                         "image_native_select")

    def invoke(self, failing=False, monitor_failure=False):
        recorder_module = load_phase_recorder()
        captured = {}

        class FakeAdapter:
            submission_started = False
            contact_span_evidence = contact_evidence()

            def __init__(self, target):
                captured["target"] = target

        class FakeMonitor:
            def __init__(self, pid, hwnd, phase_recorder=None):
                captured["monitor_args"] = (pid, hwnd)
                captured["recorder"] = phase_recorder

            def start(self):
                return self

            def stop(self):
                if monitor_failure:
                    raise RuntimeError("private monitor stop")
                captured["stop_summary"] = captured["recorder"].snapshot()
                return {
                    "background_observation_passed": True,
                    "phase_summary": captured["stop_summary"],
                }

        def collect(_adapter, *, steps, limit_per_view, deadline, phase):
            captured["collector_args"] = (steps, limit_per_view, deadline)
            phase("enter_contacts")
            phase("span_read")
            if failing:
                raise AdapterError("context_conflict")
            phase("restore_contacts")
            phase("restore_chat")
            phase("complete")
            return phase_body()

        adapter_module = SimpleNamespace(Adapter=FakeAdapter)
        monitor_module = worker_monitor_module(FakeMonitor)
        collector_module = SimpleNamespace(collect_contact_span=collect)
        request = {
            "action": "read_contact_span",
            "args": {"steps": 1, "limit_per_view": 100},
            "target": {"pid": 1, "hwnd": 2},
            "deadline": 123.0,
        }
        output = io.StringIO()
        with patch.dict(sys.modules, {
            "wxbg.adapter": adapter_module,
            "wxbg.monitor": monitor_module,
            "wxbg.contact_span": collector_module,
            "wxbg.phase_recorder": recorder_module,
        }), patch.object(sys, "stdin", io.StringIO(json.dumps(request))), \
                redirect_stdout(output):
            worker.main()
        return json.loads(output.getvalue()), captured

    def test_contact_worker_passes_phase_callback_and_collector_can_complete(self):
        response, captured = self.invoke()
        self.assertTrue(response["ok"], response)
        self.assertEqual(captured["collector_args"], (1, 100, 123.0))
        self.assertEqual(captured["stop_summary"]["phase"], "complete")
        self.assertIn("phase_summary", response["evidence"])
        self.assertNotIn("private", json.dumps(response["evidence"]))

    def test_contact_worker_does_not_mark_failed_collector_complete(self):
        response, captured = self.invoke(failing=True)
        self.assertFalse(response["ok"])
        self.assertNotEqual(captured["stop_summary"]["phase"], "complete")
        self.assertEqual(captured["stop_summary"]["phase"], "span_read")
        self.assertNotIn("private", json.dumps(response))

    def test_monitor_stop_failure_keeps_fixed_phase_evidence_and_withholds_body(self):
        response, captured = self.invoke(monitor_failure=True)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "monitor_failed")
        self.assertNotIn("result", response)
        self.assertFalse(response["evidence"]["background_observation_passed"])
        self.assertEqual(response["evidence"]["monitor_errors"], ["monitor_failed"])
        self.assertEqual(response["evidence"]["phase_summary"]["phase"], "complete")
        self.assertNotIn("private monitor stop", json.dumps(response))
        self.assertNotIn("private", json.dumps(captured["recorder"].snapshot()))


if __name__ == "__main__":
    unittest.main()
