"""Pure tests for the narrow V4 existing-file-card identity reconciler."""

import hashlib
import sys
from pathlib import Path
import unittest


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wxbg.image_row_identity import Reconciler
from wxbg.policy import AdapterError


FIXTURE_NAME = "owned-image.png"


def ref(value):
    if isinstance(value, str) and len(value) == 32:
        return value
    return (str(value) * 32)[:32]


def row(runtime, name, *, kind="mmui::ChatBubbleItemView", ref_value="a",
        rectangle=(100, 100, 300, 200)):
    return {
        "kind": kind,
        "runtime": runtime,
        "ref": ref(ref_value),
        "name": name,
        "rectangle": rectangle,
    }


def file_card(filename, runtime, *, ref_value="a", rectangle=(100, 100, 300, 200),
              progress=None, status=None):
    lines = ["檔案"]
    if progress is not None:
        lines.append(f"進度: {progress}%")
    lines.extend([filename, "1K"])
    if status is not None:
        lines.append(status)
    lines.append("微信电脑版")
    return row(runtime, "\n".join(lines), ref_value=ref_value, rectangle=rectangle)


def image_row(runtime, *, ref_value="a", rectangle=(100, 100, 300, 200)):
    return row(runtime, "图片", ref_value=ref_value, rectangle=rectangle)


def text_row(runtime, *, ref_value="a"):
    return row(runtime, "private caption", kind="mmui::ChatTextItemView",
               ref_value=ref_value)


def separator_row(runtime, *, ref_value="a"):
    return row(runtime, "10:00", kind="mmui::ChatTimeSeparatorItemView",
               ref_value=ref_value)


def chat_separator_row(runtime, name="10:00", *, ref_value="a"):
    return row(runtime, name, kind="mmui::ChatItemView", ref_value=ref_value)


class ReconcilerTests(unittest.TestCase):
    def assert_invalid(self, rows, *, fixture=FIXTURE_NAME):
        with self.assertRaises(AdapterError) as caught:
            Reconciler(rows, fixture)
        self.assertEqual(caught.exception.code, "image_identity_invalid")
        self.assertEqual(str(caught.exception), "image_identity_invalid")

    def assert_no_rebind(self, baseline, current):
        reconciler = Reconciler(baseline, FIXTURE_NAME)
        result = reconciler.reconcile(current)
        self.assertEqual(result["rebound_count"], 0)
        self.assertEqual(result["ignored_rebound_total"], 0)
        self.assertEqual(reconciler.ignored_runtime_ids,
                         frozenset(row["runtime"] for row in baseline))
        return result

    def test_baseline_and_exact_observation_return_fixed_redacted_metadata(self):
        baseline = [file_card("old.txt", "baseline-runtime")]
        reconciler = Reconciler(baseline, FIXTURE_NAME)

        self.assertEqual(reconciler.ignored_runtime_ids,
                         frozenset({"baseline-runtime"}))
        summary = reconciler.baseline_summary
        self.assertEqual(set(summary), {"row_count", "runtime_sha256", "semantic_sha256"})
        self.assertEqual(summary["row_count"], 1)
        self.assertEqual(len(summary["runtime_sha256"]), 64)
        self.assertEqual(len(summary["semantic_sha256"]), 64)

        result = reconciler.reconcile(baseline)
        self.assertEqual(set(result), {
            "row_count", "runtime_sha256", "semantic_sha256",
            "rebound_count", "ignored_rebound_total",
        })
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["runtime_sha256"], summary["runtime_sha256"])
        self.assertEqual(result["semantic_sha256"], summary["semantic_sha256"])
        self.assertEqual(result["rebound_count"], 0)
        self.assertEqual(result["ignored_rebound_total"], 0)
        summary["row_count"] = 99
        self.assertEqual(reconciler.baseline_summary["row_count"], 1)
        self.assertNotIn("old.txt", repr(summary))
        self.assertNotIn("old.txt", repr(result))

    def test_txt_pdf_zip_cards_rebind_through_the_real_card_parser(self):
        cases = (
            ("legacy.txt", {}),
            ("legacy.pdf", {"progress": 100}),
            ("legacy.zip", {"status": "上傳完成"}),
        )
        for index, (filename, options) in enumerate(cases):
            with self.subTest(filename=filename):
                baseline = [file_card(filename, f"baseline-{index}", **options)]
                alias = file_card(filename, f"alias-{index}", ref_value=str(index + 1), **options)
                reconciler = Reconciler(baseline, FIXTURE_NAME)
                result = reconciler.reconcile([alias])
                self.assertEqual(result["rebound_count"], 1)
                self.assertEqual(result["ignored_rebound_total"], 1)
                self.assertIn(alias["runtime"], reconciler.ignored_runtime_ids)
                self.assertNotIn(filename, repr(result))

    def test_stable_rebound_observation_reports_zero_new_rebounds(self):
        baseline = [file_card("legacy.txt", "baseline-runtime")]
        alias = file_card("legacy.txt", "alias-runtime", ref_value="b")
        reconciler = Reconciler(baseline, FIXTURE_NAME)

        first = reconciler.reconcile([alias])
        second = reconciler.reconcile([alias])

        self.assertEqual(first["rebound_count"], 1)
        self.assertEqual(second["rebound_count"], 0)
        self.assertEqual(second["ignored_rebound_total"], 1)
        self.assertEqual(reconciler.ignored_refs,
                         frozenset({"a" * 32, "b" * 32}))

    def test_allow_separator_updates_must_be_bool(self):
        baseline = [chat_separator_row("separator-runtime")]
        current = [chat_separator_row("separator-runtime", ref_value="a")]
        reconciler = Reconciler(baseline, FIXTURE_NAME)
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value):
                with self.assertRaises(AdapterError) as caught:
                    reconciler.reconcile(current, allow_separator_updates=value)
                self.assertEqual(caught.exception.code, "image_identity_invalid")

    def test_pre_send_separator_name_change_remains_rejected(self):
        baseline = [chat_separator_row("separator-runtime")]
        current = [chat_separator_row("separator-runtime", "11:30", ref_value="a")]
        with self.assertRaises(AdapterError) as caught:
            Reconciler(baseline, FIXTURE_NAME).reconcile(current)
        self.assertEqual(caught.exception.code, "image_identity_invalid")

    def test_post_send_separator_name_change_is_allowed_without_alias(self):
        baseline = [chat_separator_row("separator-runtime")]
        current = [chat_separator_row("separator-runtime", "11:30", ref_value="a")]
        reconciler = Reconciler(baseline, FIXTURE_NAME)
        result = reconciler.reconcile(current, allow_separator_updates=True)
        self.assertEqual(result["rebound_count"], 0)
        self.assertEqual(result["ignored_rebound_total"], 0)
        self.assertEqual(reconciler.ignored_runtime_ids,
                         frozenset({"separator-runtime"}))

    def test_post_send_separator_requires_known_runtime_owner(self):
        baseline = [chat_separator_row("separator-runtime", ref_value="a")]
        cases = (
            chat_separator_row("new-runtime", "11:30", ref_value="a"),
        )
        for current in cases:
            with self.subTest(runtime=current["runtime"], ref=current["ref"]):
                with self.assertRaises(AdapterError) as caught:
                    Reconciler(baseline, FIXTURE_NAME).reconcile(
                        [current], allow_separator_updates=True
                    )
                self.assertEqual(caught.exception.code, "image_identity_invalid")

    def test_post_send_separator_requires_strict_time_names(self):
        baseline = [chat_separator_row("separator-runtime")]
        for name in ("notes", "25:99"):
            with self.subTest(name=name):
                current = chat_separator_row("separator-runtime", name, ref_value="a")
                with self.assertRaises(AdapterError) as caught:
                    Reconciler(baseline, FIXTURE_NAME).reconcile(
                        [current], allow_separator_updates=True
                    )
                self.assertEqual(caught.exception.code, "image_identity_invalid")

    def test_post_send_file_image_and_class_changes_remain_rejected(self):
        cases = (
            (file_card("legacy.txt", "file-runtime"),
             file_card("changed.txt", "file-runtime", ref_value="a")),
            (image_row("image-runtime"),
             row("image-runtime", "changed image", ref_value="a")),
            (chat_separator_row("class-runtime"),
             row("class-runtime", "changed class", kind="mmui::ChatTextItemView",
                 ref_value="a")),
        )
        for baseline_row, current_row in cases:
            with self.subTest(kind=baseline_row["kind"]):
                with self.assertRaises(AdapterError) as caught:
                    Reconciler([baseline_row], FIXTURE_NAME).reconcile(
                        [current_row], allow_separator_updates=True
                    )
                self.assertEqual(caught.exception.code, "image_identity_invalid")

    def test_post_send_separator_owner_conflict_is_rejected(self):
        baseline = [
            chat_separator_row("separator-runtime", ref_value="a"),
            text_row("other-runtime", ref_value="b"),
        ]
        conflicting = row("separator-runtime", "changed separator", ref_value="b")
        with self.assertRaises(AdapterError) as caught:
            Reconciler(baseline, FIXTURE_NAME).reconcile(
                [conflicting], allow_separator_updates=True
            )
        self.assertEqual(caught.exception.code, "image_identity_invalid")

    def test_image_png_jpg_text_and_separator_rows_are_not_rebound(self):
        cases = (
            (image_row("image-alias", ref_value="b"), image_row("image-base")),
            (file_card("legacy.png", "png-alias", ref_value="b"),
             file_card("legacy.png", "png-base")),
            (file_card("legacy.jpg", "jpg-alias", ref_value="b"),
             file_card("legacy.jpg", "jpg-base")),
            (text_row("text-alias", ref_value="b"), text_row("text-base")),
            (separator_row("separator-alias", ref_value="b"),
             separator_row("separator-base")),
        )
        for current, baseline_row in cases:
            with self.subTest(kind=baseline_row["kind"], name=baseline_row["name"]):
                self.assert_no_rebind([baseline_row], [current])

    def test_exact_unique_text_scene_rebuild_is_rebound_only_when_opted_in(self):
        baseline = [text_row("old-text-runtime", ref_value="a")]
        rebuilt = [text_row("rebuilt-text-runtime", ref_value="b")]
        reconciler = Reconciler(baseline, FIXTURE_NAME)

        summary = reconciler.reconcile(rebuilt, allow_scene_rebind=True)

        self.assertEqual(summary["rebound_count"], 1)
        self.assertEqual(summary["ignored_rebound_total"], 1)
        self.assertIn("rebuilt-text-runtime", reconciler.ignored_runtime_ids)
        self.assertIn("b" * 32, reconciler.ignored_refs)

    def test_text_scene_rebuild_still_rejects_changed_content_or_geometry(self):
        baseline = [text_row("old-text-runtime", ref_value="a")]
        for rebuilt in (
            text_row("new-text-runtime", ref_value="b") | {"name": "different text"},
            text_row("new-text-runtime", ref_value="b") | {"rectangle": (101, 100, 301, 200)},
        ):
            with self.subTest(rebuilt=rebuilt["name"]):
                reconciler = Reconciler(baseline, FIXTURE_NAME)
                summary = reconciler.reconcile([rebuilt], allow_scene_rebind=True)
                self.assertEqual(summary["rebound_count"], 0)
                self.assertNotIn(rebuilt["runtime"], reconciler.ignored_runtime_ids)

    def test_duplicate_semantics_are_ambiguous_and_not_rebound(self):
        baseline = [
            file_card("legacy.txt", "baseline-a", ref_value="a"),
            file_card("legacy.txt", "baseline-b", ref_value="b"),
        ]
        current = [
            file_card("legacy.txt", "alias-a", ref_value="c"),
            file_card("legacy.txt", "baseline-b", ref_value="b"),
        ]
        result = self.assert_no_rebind(baseline, current)
        self.assertEqual(result["row_count"], 2)

    def test_appended_identical_row_is_not_suppressed(self):
        baseline = [file_card("legacy.txt", "baseline-runtime", ref_value="a")]
        appended = file_card("legacy.txt", "appended-runtime", ref_value="b")
        result = self.assert_no_rebind(baseline, baseline + [appended])
        self.assertEqual(result["row_count"], 2)
        self.assertNotIn(appended["runtime"], Reconciler(baseline, FIXTURE_NAME).ignored_runtime_ids)

    def test_content_geometry_order_and_count_changes_are_not_rebound(self):
        baseline = [
            file_card("legacy.txt", "baseline-a", ref_value="a"),
            text_row("baseline-b", ref_value="b"),
        ]
        cases = (
            [file_card("changed.txt", "alias-a", ref_value="c"), text_row("baseline-b", ref_value="b")],
            [file_card("legacy.txt", "alias-a", ref_value="c", rectangle=(101, 100, 301, 200)),
             text_row("baseline-b", ref_value="b")],
            [text_row("alias-b", ref_value="c"), file_card("legacy.txt", "alias-a", ref_value="d")],
            [file_card("legacy.txt", "alias-a", ref_value="c")],
            [],
        )
        for current in cases:
            with self.subTest(current=current):
                self.assert_no_rebind(baseline, current)

    def test_mixed_eligible_and_ineligible_rebind_is_all_or_nothing(self):
        baseline = [
            file_card("legacy.txt", "baseline-a", ref_value="a"),
            image_row("baseline-b", ref_value="b"),
        ]
        current = [
            file_card("legacy.txt", "alias-a", ref_value="c"),
            image_row("alias-b", ref_value="d"),
        ]
        result = self.assert_no_rebind(baseline, current)
        self.assertEqual(result["row_count"], 2)

    def test_accepted_alias_persists_through_geometry_change_and_appended_image(self):
        baseline = [file_card("legacy.txt", "baseline-runtime", ref_value="a")]
        alias = file_card("legacy.txt", "alias-runtime", ref_value="b")
        appended_image = image_row("new-image-runtime", ref_value="c")
        reconciler = Reconciler(baseline, FIXTURE_NAME)

        first = reconciler.reconcile([alias])
        final = reconciler.reconcile([
            file_card("legacy.txt", "alias-runtime", ref_value="b",
                      rectangle=(110, 110, 310, 210)),
            appended_image,
        ])

        self.assertEqual(first["rebound_count"], 1)
        self.assertEqual(final["rebound_count"], 0)
        self.assertEqual(final["ignored_rebound_total"], 1)
        self.assertIn("alias-runtime", reconciler.ignored_runtime_ids)
        self.assertNotIn(appended_image["runtime"], reconciler.ignored_runtime_ids)
        self.assertEqual(final["row_count"], 2)

    def test_accepted_alias_reused_for_changed_content_is_rejected_without_raw_detail(self):
        baseline = [file_card("legacy.txt", "baseline-runtime", ref_value="a")]
        reconciler = Reconciler(baseline, FIXTURE_NAME)
        reconciler.reconcile([file_card("legacy.txt", "alias-runtime", ref_value="b")])

        changed = file_card("other.txt", "alias-runtime", ref_value="b")
        with self.assertRaises(AdapterError) as caught:
            reconciler.reconcile([changed])
        self.assertEqual(caught.exception.code, "image_identity_invalid")
        self.assertEqual(str(caught.exception), "image_identity_invalid")
        self.assertNotIn("other.txt", str(caught.exception))

    def test_bounded_validation_rejects_bad_rows_and_accepts_rectangle_lists(self):
        valid = file_card("legacy.txt", "baseline-runtime")
        accepted = dict(valid, rectangle=[100, 100, 300, 200])
        self.assertEqual(Reconciler([accepted], FIXTURE_NAME).baseline_summary["row_count"], 1)
        invalid_rows = [
            None,
            {key: value for key, value in valid.items() if key != "name"},
            dict(valid, extra="not allowed"),
            dict(valid, kind="mmui::OtherView"),
            dict(valid, runtime=""),
            dict(valid, ref="A" * 32),
            dict(valid, name="x" * 4097),
            dict(valid, rectangle=[1, 2, 3]),
            dict(valid, rectangle=[1, 2, 3, True]),
            dict(valid, rectangle=[1, 2, 3, 32768]),
            dict(valid, runtime="x" * 257),
        ]
        for value in invalid_rows:
            with self.subTest(value_type=type(value).__name__):
                self.assert_invalid([value])

        duplicate_runtime = [valid, dict(valid, ref=ref("b"))]
        duplicate_ref = [valid, dict(valid, runtime="other-runtime")]
        self.assert_invalid(duplicate_runtime)
        self.assert_invalid(duplicate_ref)
        too_many = [
            row(f"runtime-{index}", "text", kind="mmui::ChatTextItemView",
                ref_value=f"{index:032x}")
            for index in range(257)
        ]
        self.assert_invalid(too_many)
        self.assert_invalid([valid], fixture="")

    def test_alias_cap_is_baseline_count_times_five_including_initial_identity(self):
        baseline = [file_card("legacy.txt", "baseline-runtime", ref_value="a")]
        reconciler = Reconciler(baseline, FIXTURE_NAME)
        for index in range(1, 5):
            result = reconciler.reconcile([
                file_card("legacy.txt", f"alias-{index}", ref_value=f"{index + 1:032x}")
            ])
            self.assertEqual(result["ignored_rebound_total"], index)
        self.assertEqual(len(reconciler.ignored_runtime_ids), 5)

        with self.assertRaises(AdapterError) as caught:
            reconciler.reconcile([
                file_card("legacy.txt", "alias-5", ref_value="f" * 32)
            ])
        self.assertEqual(caught.exception.code, "image_identity_invalid")
        self.assertEqual(len(reconciler.ignored_runtime_ids), 5)

    def test_public_metadata_contains_hashes_not_raw_row_content(self):
        baseline = [file_card("secret-name.txt", "secret-runtime", ref_value="a")]
        reconciler = Reconciler(baseline, FIXTURE_NAME)
        result = reconciler.reconcile([
            file_card("secret-name.txt", "secret-alias", ref_value="b")
        ])
        public = {"summary": reconciler.baseline_summary, "result": result}
        rendered = repr(public)
        for raw in ("secret-name.txt", "secret-runtime", "secret-alias"):
            self.assertNotIn(raw, rendered)
        self.assertEqual(set(result), {
            "row_count", "runtime_sha256", "semantic_sha256",
            "rebound_count", "ignored_rebound_total",
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
