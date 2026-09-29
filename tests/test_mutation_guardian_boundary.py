"""Synthetic guardian checks for mutation evidence; no Weixin client is used."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from wxbg import supervisor
from test_gate import FakeBackend


MUTATIONS = ("send_text", "send_at_username", "send_file", "set_draft")


def monitor_evidence():
    snapshot = {
        "foreground": 99,
        "cursor": [10, 20],
        "cursor_api": "GetCursorPos",
        "cursor_dpi_context": "per_monitor_v2",
        "cursor_coordinate_space": "screen_coordinates_under_pm_v2",
        "clipboard_sequence": 7,
        "minimized": True,
        "visible_windows": [444],
        "capture": 0,
    }
    return {
        "observations": 2,
        "poll_interval_ms": 10,
        "before": deepcopy(snapshot),
        "after": deepcopy(snapshot),
        "foreground_changed": False,
        "clipboard_changed": False,
        "cursor_changed": False,
        "new_visible_windows": [],
        "target_restored": False,
        "capture_observed": False,
        "monitor_errors": [],
        "background_observation_passed": True,
        "verification_limit": "10ms sampling cannot prove absence of shorter transients; cursor movement can be user activity",
    }


class MutationGuardianBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.backend = FakeBackend()
        self.backend.state_dir = self.directory

    def run_guardian(self, action, evidence):
        reply = {"ok": True, "result": {"ok": True, "status": "submitted"}}
        if evidence is not None:
            reply["evidence"] = evidence
        return supervisor.run_request(
            {"action": action, "args": {}}, state_dir=self.directory,
            backend=self.backend, worker_runner=lambda *_: deepcopy(reply),
            mutex_factory=lambda *_: nullcontext(),
        )

    def test_valid_background_evidence_keeps_mutation_success(self):
        for action in MUTATIONS:
            with self.subTest(action=action):
                response = self.run_guardian(action, monitor_evidence())
                self.assertTrue(response["ok"], response)
                self.assertTrue(response["cleanup"]["restored"])

    def test_missing_or_inconsistent_monitor_cannot_confirm_mutation(self):
        bad_cases = {
            "missing": lambda e: None,
            "declared_failed": lambda e: {**e, "background_observation_passed": False},
            "foreground_flag": lambda e: {**e, "foreground_changed": True},
            "foreground_snapshot": lambda e: {**e, "after": {**e["after"], "foreground": 100}},
            "zero_observations": lambda e: {**e, "observations": 0},
            "one_observation": lambda e: {**e, "observations": 1},
            "new_window": lambda e: {**e, "new_visible_windows": [999]},
            "monitor_error": lambda e: {**e, "monitor_errors": ["private error"]},
            "restored_target": lambda e: {**e, "after": {**e["after"], "minimized": False}},
        }
        for action in MUTATIONS:
            for label, make_bad in bad_cases.items():
                with self.subTest(action=action, case=label):
                    response = self.run_guardian(action, make_bad(monitor_evidence()))
                    self.assertFalse(response["ok"], response)
                    self.assertTrue(response["error"]["outcome_unknown"], response)
                    self.assertNotIn("result", response)


if __name__ == "__main__":
    unittest.main()
