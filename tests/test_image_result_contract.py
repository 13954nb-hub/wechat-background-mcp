"""Pure public-contract tests for the local image transition result."""

from copy import deepcopy
import unittest

try:
    from wxbg import image_result_contract as contract
except ImportError:
    contract = None


SESSION_REF = "a" * 32
RUNTIME_SHA = "b" * 64
SEMANTIC_SHA = "c" * 64


def frame(index=1, **changes):
    value = {
        "index": index,
        "draft_kind": "empty",
        "row_count": 6,
        "runtime_sha256": RUNTIME_SHA,
        "semantic_sha256": SEMANTIC_SHA,
        "rebound_count": 0,
        "ignored_rebound_total": 0,
        "new_rows_count": 2,
        "rows_seen": True,
        "image_novelty": True,
        "embedded_observations": 0,
        "stable_count": 2,
    }
    value.update(changes)
    return value


def image_evidence(*, send_clicks=1, new_rows=2, **changes):
    final_frames = []
    selection_frames = []
    if send_clicks:
        final_frames = [
            frame(1, new_rows_count=new_rows),
            frame(2, new_rows_count=new_rows, stable_count=2),
        ]
    else:
        selection_frames = [
            frame(1, new_rows_count=new_rows),
            frame(2, new_rows_count=new_rows, stable_count=2),
        ]
    value = {
        "version": 1,
        "baseline": {
            "row_count": 6,
            "runtime_sha256": RUNTIME_SHA,
            "semantic_sha256": SEMANTIC_SHA,
        },
        "selection_frames": selection_frames,
        "final_frames": final_frames,
    }
    value.update(changes)
    return value


def result(*, session_ref=SESSION_REF, send_clicks=1, new_rows=2, **changes):
    value = {
        "ok": True,
        "status": "local_image_transition_observed",
        "verification_level": "stable_local_image_ui_transition",
        "counts": {
            "native_selections": 1,
            "send_clicks": send_clicks,
            "new_rows": new_rows,
        },
        "refs": {"conversation": session_ref},
        "background_mode": "minimized",
        "remote_receipt_verified": False,
        "upload_status": "unknown",
    }
    value.update(changes)
    return value


def native_evidence(**changes):
    value = {
        "matches": 1,
        "accepted_shows": 1,
        "shows": 0,
        "live": 0,
        "installed": 0,
        "active_filter": 0,
        "protection_restored": 1,
        "cleanup_unresolved": 0,
        "released": True,
        "grant_revoked": True,
        "error": 0,
        "passed": True,
        "hook_removed": True,
        "local_module_released": True,
        "remote_module_present": True,
        "primary_error": None,
        "cleanup_errors": [],
        "submission_started": True,
    }
    value.update(changes)
    return value


def background_evidence(**changes):
    state = {
        "minimized": True,
        "capture": 0,
        "foreground": 5,
        "clipboard_sequence": 8,
        "cursor": [10, 20],
        "visible_windows": [2],
        "cursor_api": "GetCursorPos",
        "cursor_dpi_context": "per_monitor_v2",
        "cursor_coordinate_space": "screen_coordinates_under_pm_v2",
    }
    value = {
        "background_observation_passed": True,
        "observations": 3,
        "foreground_changed": False,
        "clipboard_changed": False,
        "cursor_changed": False,
        "target_restored": False,
        "capture_observed": False,
        "new_visible_windows": [],
        "monitor_errors": [],
        "before": deepcopy(state),
        "after": deepcopy(state),
    }
    value.update(changes)
    return value


def cleanup(**changes):
    value = {
        "status": "restored",
        "restored": True,
        "observed": 0,
        "errors": [],
    }
    value.update(changes)
    return value


def response(*, send_clicks=1, new_rows=2, **changes):
    value = {
        "ok": True,
        "worker_started": True,
        "result": result(send_clicks=send_clicks, new_rows=new_rows),
        "image_evidence_valid": True,
        "image_evidence": image_evidence(send_clicks=send_clicks, new_rows=new_rows),
        "native_evidence": native_evidence(),
        "evidence": background_evidence(),
        "cleanup": cleanup(),
    }
    value.update(changes)
    return value


class ImageResultContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(contract, "image_result_contract.py is missing")

    def test_accepts_zero_or_one_send_with_the_matching_trace_phase(self):
        for send_clicks in (0, 1):
            with self.subTest(send_clicks=send_clicks):
                self.assertTrue(contract.valid_image_response(
                    response(send_clicks=send_clicks), SESSION_REF
                ))

    def test_rebuilds_exact_public_summary_without_aliases_or_private_body(self):
        value = result(send_clicks=0, new_rows=3)
        summary = contract.image_journal_summary(value)

        self.assertEqual(set(summary), {
            "ok", "status", "verification_level", "counts", "refs",
            "background_mode", "remote_receipt_verified", "upload_status",
        })
        self.assertEqual(summary, value)
        self.assertIsNot(summary, value)
        self.assertIsNot(summary["counts"], value["counts"])
        self.assertIsNot(summary["refs"], value["refs"])
        value["counts"]["new_rows"] = 8
        value["refs"]["conversation"] = "d" * 32
        self.assertEqual(summary["counts"]["new_rows"], 3)
        self.assertEqual(summary["refs"]["conversation"], SESSION_REF)
        self.assertNotIn("rows", summary)
        self.assertNotIn("path", summary)
        self.assertNotIn("rawtrace", summary)

    def test_journal_summary_uses_one_fixed_invalid_value_error_code(self):
        invalid = result()
        invalid["private"] = "raw"
        with self.assertRaises(ValueError) as caught:
            contract.image_journal_summary(invalid)
        self.assertEqual(str(caught.exception), "image_result_invalid")

    def test_missing_each_response_gate_is_rejected(self):
        for key in (
            "ok", "worker_started", "result", "image_evidence_valid",
            "image_evidence", "native_evidence", "evidence", "cleanup",
        ):
            with self.subTest(key=key):
                candidate = response()
                candidate.pop(key)
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

    def test_result_is_strict_and_rejects_wrong_ref_or_types(self):
        cases = []
        private_top_level = response()
        private_top_level["private_top_level"] = "raw"
        cases.append(private_top_level)
        extra = response()
        extra["result"]["private"] = "raw"
        cases.append(extra)
        extra_count = response()
        extra_count["result"]["counts"]["private"] = 1
        cases.append(extra_count)
        wrong_ref = response()
        wrong_ref["result"]["refs"]["conversation"] = "A" * 32
        cases.append(wrong_ref)
        bool_count = response()
        bool_count["result"]["counts"]["send_clicks"] = True
        cases.append(bool_count)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

        for ref in ("b" * 31, "A" * 32, True):
            candidate = response()
            candidate["result"]["refs"]["conversation"] = ref
            with self.subTest(ref=ref):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

    def test_native_evidence_requires_typed_full_terminal_contract(self):
        required_ints = (
            "matches", "accepted_shows", "shows", "live", "installed",
            "active_filter", "protection_restored", "cleanup_unresolved", "error",
        )
        required_bools = (
            "released", "grant_revoked", "passed", "hook_removed",
            "local_module_released", "remote_module_present", "submission_started",
        )
        for key in required_ints + required_bools + (
            "primary_error", "cleanup_errors"
        ):
            with self.subTest(key=key):
                candidate = response()
                candidate["native_evidence"].pop(key)
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

        for key in required_ints:
            candidate = response()
            candidate["native_evidence"][key] = True
            with self.subTest(key=key):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))
        for key in required_bools:
            candidate = response()
            candidate["native_evidence"][key] = 1
            with self.subTest(key=key):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

        for changes in (
            {"matches": 2},
            {"shows": 1},
            {"released": False},
            {"primary_error": {"code": "private"}},
            {"cleanup_errors": [{"code": "private"}]},
            {"submission_started": False},
        ):
            candidate = response()
            candidate["native_evidence"].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

    def test_cleanup_requires_restored_zero_observation_and_no_errors(self):
        for changes in (
            {"restored": False},
            {"restored": 1},
            {"observed": True},
            {"observed": 1},
            {"observed": -1},
            {"errors": [{"code": "private"}]},
            {"errors": None},
        ):
            candidate = response(cleanup=cleanup(**changes))
            with self.subTest(changes=changes):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

    def test_trace_must_have_valid_baseline_and_no_path_or_raw_fields(self):
        candidates = []
        missing = response()
        missing.pop("image_evidence")
        candidates.append(missing)
        invalid_flag = response()
        invalid_flag["image_evidence_valid"] = False
        candidates.append(invalid_flag)
        no_baseline = response()
        no_baseline["image_evidence"]["baseline"] = None
        candidates.append(no_baseline)
        path_field = response()
        path_field["image_evidence"]["path"] = r"C:\private\image.png"
        candidates.append(path_field)
        frame_field = response()
        frame_field["image_evidence"]["final_frames"][0]["path"] = r"C:\private\image.png"
        candidates.append(frame_field)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

    def test_trace_requires_two_novel_empty_stable_matching_frames_and_rows(self):
        mutations = (
            {"image_novelty": False},
            {"draft_kind": "embedded"},
            {"stable_count": 1},
            {"runtime_sha256": "d" * 64},
            {"semantic_sha256": "d" * 64},
            {"row_count": 7},
            {"new_rows_count": 1},
        )
        for send_clicks in (0, 1):
            for changes in mutations:
                with self.subTest(send_clicks=send_clicks, changes=changes):
                    candidate = response(send_clicks=send_clicks, new_rows=2)
                    frames = candidate["image_evidence"][
                        "final_frames" if send_clicks else "selection_frames"
                    ]
                    frames[-1].update(changes)
                    self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

            one_frame = response(send_clicks=send_clicks)
            stage = "final_frames" if send_clicks else "selection_frames"
            one_frame["image_evidence"][stage].pop()
            self.assertFalse(contract.valid_image_response(one_frame, SESSION_REF))

    def test_background_allows_cursor_change_as_typed_telemetry(self):
        candidate = response()
        candidate["evidence"]["cursor_changed"] = True
        candidate["evidence"]["after"]["cursor"] = [30, 40]
        self.assertTrue(contract.valid_image_response(candidate, SESSION_REF))

    def test_background_rejects_focus_change_even_when_cursor_changes(self):
        for key in ("foreground", "clipboard_sequence", "visible_windows"):
            candidate = response()
            candidate["evidence"]["cursor_changed"] = True
            candidate["evidence"]["after"]["cursor"] = [30, 40]
            candidate["evidence"]["after"][key] = (
                [3] if key == "visible_windows" else 100
            )
            with self.subTest(key=key):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

    def test_background_requires_typed_dpi_and_observation_gates(self):
        for changes in (
            {"background_observation_passed": False},
            {"observations": True},
            {"new_visible_windows": [3]},
            {"monitor_errors": ["private"]},
            {"before": {"minimized": False}},
            {"before": {"capture": True}},
            {"before": {"cursor_dpi_context": "unaware"}},
            {"after": {"cursor": [1]}},
        ):
            candidate = response()
            candidate["evidence"].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(contract.valid_image_response(candidate, SESSION_REF))

    def test_validation_is_read_only_for_all_nested_inputs(self):
        candidate = response(send_clicks=0, new_rows=4)
        before = deepcopy(candidate)
        self.assertTrue(contract.valid_image_response(candidate, SESSION_REF))
        self.assertEqual(candidate, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
