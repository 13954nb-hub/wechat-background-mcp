import copy
import unittest

try:
    from wxbg import image_observation_contract as contract
except ImportError:
    contract = None


HASH = "a" * 64
BASELINE_KEYS = {"row_count", "runtime_sha256", "semantic_sha256"}
FRAME_KEYS = {
    "index",
    "draft_kind",
    "row_count",
    "runtime_sha256",
    "semantic_sha256",
    "rebound_count",
    "ignored_rebound_total",
    "new_rows_count",
        "rows_seen",
        "image_novelty",
        "embedded_observations",
    "stable_count",
}


def baseline(**overrides):
    value = {
        "row_count": 2,
        "runtime_sha256": HASH,
        "semantic_sha256": "b" * 64,
    }
    value.update(overrides)
    return value


def frame(index=1, **overrides):
    value = {
        "index": index,
        "draft_kind": "empty",
        "row_count": 2,
        "runtime_sha256": HASH,
        "semantic_sha256": "b" * 64,
        "rebound_count": 0,
        "ignored_rebound_total": 0,
        "new_rows_count": 0,
        "rows_seen": True,
        "image_novelty": False,
        "embedded_observations": 0,
        "stable_count": 2,
    }
    value.update(overrides)
    return value


def selection_diagnostics(**overrides):
    value = {
        "draft_initial_kind": "embedded",
        "draft_first_recheck_kind": "embedded",
        "scene_rebind_enabled": True,
        "same_scene_content": True,
        "same_scene_geometry": True,
        "same_scene_runtime": False,
        "new_row_kind_counts": {
            "text": 0, "bubble": 0, "bubble_refer": 0,
            "separator": 0, "other": 0,
        },
        "new_row_label_counts": {
            "image_label": 0, "owned_file_card": 0, "other": 0,
        },
        "decision": "presend",
    }
    value.update(overrides)
    return value


def evidence(selection_frames=None, final_frames=None, baseline_value=None):
    return {
        "version": 1,
        "baseline": baseline() if baseline_value is None else baseline_value,
        "selection_frames": [] if selection_frames is None else selection_frames,
        "final_frames": [] if final_frames is None else final_frames,
    }


class ImageObservationContractTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(contract, "image_observation_contract.py is missing")

    def test_accepts_closed_selection_diagnostics_without_raw_text(self):
        value = evidence(selection_frames=[frame(1, draft_kind="embedded",
                                                  diagnostics=selection_diagnostics())])

        fixed, valid = contract.normalize_image_evidence(value)

        self.assertTrue(valid)
        self.assertEqual(fixed, value)
        self.assertIsNot(fixed["selection_frames"][0]["diagnostics"],
                         value["selection_frames"][0]["diagnostics"])
        self.assertNotIn("private", repr(fixed))

    def test_rejects_unbounded_selection_diagnostics_and_final_diagnostics(self):
        bad_values = []
        raw = selection_diagnostics(raw=r"C:\private\image.png")
        bad_values.append(evidence(selection_frames=[frame(1, diagnostics=raw)]))
        changed_keys = selection_diagnostics(new_row_kind_counts={
            "text": 0, "bubble": 0, "bubble_refer": 0,
            "separator": 0, "private": 0,
        })
        bad_values.append(evidence(selection_frames=[frame(1, diagnostics=changed_keys)]))
        bad_values.append(evidence(selection_frames=[frame(1,
            diagnostics=selection_diagnostics(decision="send_anyway"))]))
        bad_values.append(evidence(selection_frames=[frame(1,
            diagnostics=selection_diagnostics(decision=[]))]))
        bad_values.append(evidence(selection_frames=[frame(1,
            diagnostics=selection_diagnostics(draft_initial_kind=[]))]))
        bad_values.append(evidence(selection_frames=[frame(1,
            diagnostics=selection_diagnostics(scene_rebind_enabled=1))]))
        bad_values.append(evidence(selection_frames=[frame(1,
            diagnostics=selection_diagnostics(new_row_kind_counts={
                "text": 1, "bubble": 0, "bubble_refer": 0,
                "separator": 0, "other": 0,
            }))]))
        bad_values.append(evidence(final_frames=[frame(1,
            diagnostics=selection_diagnostics())]))

        for value in bad_values:
            with self.subTest(value=value):
                fixed, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)
                self.assertEqual(fixed["selection_frames"], [])
                self.assertEqual(fixed["final_frames"], [])

    def test_accepts_zero_through_four_selection_frames(self):
        for count in range(5):
            with self.subTest(count=count):
                value = evidence(
                    selection_frames=[frame(index) for index in range(1, count + 1)],
                    baseline_value=None if count == 0 else baseline(),
                )
                fixed, valid = contract.normalize_image_evidence(value)

                self.assertTrue(valid)
                self.assertEqual(fixed, value)
                self.assertEqual([item["index"] for item in fixed["selection_frames"]],
                                 list(range(1, count + 1)))

    def test_accepts_four_final_frames_and_independent_indices(self):
        value = evidence(
            selection_frames=[frame(1, draft_kind="embedded")],
            final_frames=[frame(index, draft_kind="other") for index in range(1, 5)],
        )

        fixed, valid = contract.normalize_image_evidence(value)

        self.assertTrue(valid)
        self.assertEqual([item["index"] for item in fixed["selection_frames"]], [1])
        self.assertEqual([item["index"] for item in fixed["final_frames"]], [1, 2, 3, 4])

    def test_baseline_none_requires_empty_frame_lists(self):
        value = evidence(selection_frames=[frame(1)], baseline_value=None)
        value["baseline"] = None

        fixed, valid = contract.normalize_image_evidence(value)

        self.assertFalse(valid)
        self.assertEqual(fixed, {
            "version": 1,
            "baseline": None,
            "selection_frames": [],
            "final_frames": [],
        })

    def test_rebuilds_a_copy_without_aliases(self):
        value = evidence(
            selection_frames=[frame(1)],
            final_frames=[frame(1, draft_kind="embedded")],
        )
        fixed, valid = contract.normalize_image_evidence(value)
        snapshot = copy.deepcopy(fixed)

        self.assertTrue(valid)
        self.assertIsNot(fixed, value)
        self.assertIsNot(fixed["baseline"], value["baseline"])
        self.assertIsNot(fixed["selection_frames"], value["selection_frames"])
        self.assertIsNot(fixed["selection_frames"][0], value["selection_frames"][0])
        self.assertIsNot(fixed["final_frames"], value["final_frames"])
        self.assertIsNot(fixed["final_frames"][0], value["final_frames"][0])

        value["baseline"]["row_count"] = 99
        value["selection_frames"][0]["draft_kind"] = "other"
        value["final_frames"].clear()
        self.assertEqual(fixed, snapshot)

    def test_invalid_returns_fixed_redacted_value(self):
        value = evidence()
        value["raw"] = r"C:\private\image.png"

        fixed, valid = contract.normalize_image_evidence(value)

        self.assertFalse(valid)
        self.assertEqual(fixed, {
            "version": 1,
            "baseline": None,
            "selection_frames": [],
            "final_frames": [],
        })

    def test_rejects_extra_raw_and_path_keys_at_each_level(self):
        cases = []
        top_level = evidence()
        top_level["raw"] = "private"
        cases.append(top_level)

        baseline_extra = evidence()
        baseline_extra["baseline"]["path"] = r"C:\private\image.png"
        cases.append(baseline_extra)

        frame_extra = evidence(selection_frames=[frame(1)])
        frame_extra["selection_frames"][0]["raw"] = "private"
        cases.append(frame_extra)

        for value in cases:
            with self.subTest(value=value):
                fixed, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)
                self.assertEqual(fixed["selection_frames"], [])
                self.assertEqual(fixed["final_frames"], [])

    def test_rejects_invalid_hashes(self):
        cases = [
            evidence(baseline_value=baseline(runtime_sha256="A" * 64)),
            evidence(baseline_value=baseline(semantic_sha256="g" * 64)),
            evidence(selection_frames=[frame(1, runtime_sha256="g" * 64)]),
            evidence(selection_frames=[frame(1, semantic_sha256="a" * 63)]),
            evidence(selection_frames=[frame(1, runtime_sha256="A" * 64)]),
        ]

        for value in cases:
            with self.subTest(value=value):
                _, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)

    def test_rejects_gaps_out_of_range_indices_and_more_than_four_frames(self):
        cases = [
            evidence(selection_frames=[frame(1), frame(3)]),
            evidence(selection_frames=[frame(0)]),
            evidence(final_frames=[frame(5)]),
            evidence(selection_frames=[frame(index) for index in range(1, 6)]),
        ]

        for value in cases:
            with self.subTest(value=value):
                _, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)

    def test_rejects_missing_or_extra_schema_keys(self):
        missing = evidence()
        del missing["final_frames"]
        extra_frame = evidence(selection_frames=[frame(1)])
        extra_frame["selection_frames"][0]["private"] = "x"
        extra_baseline = evidence()
        extra_baseline["baseline"]["private"] = "x"

        for value in (missing, extra_frame, extra_baseline):
            with self.subTest(value=value):
                _, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)

    def test_all_integer_fields_reject_booleans(self):
        cases = []
        top = evidence()
        top["version"] = True
        cases.append(top)

        base = evidence()
        base["baseline"]["row_count"] = True
        cases.append(base)

        for key in (
            "index",
            "row_count",
            "rebound_count",
            "ignored_rebound_total",
            "new_rows_count",
            "embedded_observations",
            "stable_count",
        ):
            value = evidence(selection_frames=[frame(1)])
            value["selection_frames"][0][key] = True
            cases.append(value)

        for value in cases:
            with self.subTest(value=value):
                _, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)

    def test_rejects_out_of_range_integer_fields(self):
        cases = [
            evidence(baseline_value=baseline(row_count=-1)),
            evidence(baseline_value=baseline(row_count=257)),
            evidence(selection_frames=[frame(1, row_count=-1)]),
            evidence(selection_frames=[frame(1, row_count=257)]),
            evidence(selection_frames=[frame(1, rebound_count=257)]),
            evidence(selection_frames=[frame(1, ignored_rebound_total=1025)]),
            evidence(selection_frames=[frame(1, new_rows_count=9)]),
            evidence(selection_frames=[frame(1, embedded_observations=5)]),
            evidence(selection_frames=[frame(1, stable_count=5)]),
        ]

        for value in cases:
            with self.subTest(value=value):
                _, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)

    def test_rejects_non_boolean_rows_seen_and_unknown_draft_kind(self):
        for value in (
            evidence(selection_frames=[frame(1, rows_seen=1)]),
            evidence(selection_frames=[frame(1, rows_seen=None)]),
            evidence(selection_frames=[frame(1, draft_kind="pending")]),
        ):
            with self.subTest(value=value):
                _, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)

    def test_image_novelty_accepts_bools_and_rejects_integer_one(self):
        for novelty in (False, True):
            value = evidence(selection_frames=[frame(1, image_novelty=novelty)])
            fixed, valid = contract.normalize_image_evidence(value)
            self.assertTrue(valid)
            self.assertIs(fixed["selection_frames"][0]["image_novelty"], novelty)

        invalid = evidence(selection_frames=[frame(1, image_novelty=1)])
        _, valid = contract.normalize_image_evidence(invalid)
        self.assertFalse(valid)

    def test_rejects_non_container_and_non_list_shapes(self):
        for value in (None, [], "text", 1, {"version": 1}):
            with self.subTest(value=value):
                fixed, valid = contract.normalize_image_evidence(value)
                self.assertFalse(valid)
                self.assertEqual(fixed["baseline"], None)

        for key, bad_value in (("selection_frames", ()), ("final_frames", {})):
            value = evidence()
            value[key] = bad_value
            _, valid = contract.normalize_image_evidence(value)
            self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
