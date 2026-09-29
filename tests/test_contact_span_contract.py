from __future__ import annotations

import sys
from pathlib import Path
import unittest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from wxbg import contact_span_contract as contract  # noqa: E402


def evidence(steps: int = 2) -> dict:
    return {
        "mode": "bounded_contacts_span",
        "origin": "observed_top",
        "requested_steps": steps,
        "delivery_started": True,
        "entered": True,
        "attempted_down_steps": steps,
        "completed_steps": steps,
        "observed_view_count": steps + 1,
        "attempted_restore_steps": steps,
        "viewport_settled": True,
        "contacts_view_restored": True,
        "conversation_restored": True,
        "draft_preserved": True,
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "primary_error_code": None,
        "cleanup_error_code": None,
    }


def row(text: str) -> dict:
    return {"display_text": text}


def body(steps: int = 2, limit: int = 2, changed_flags=None) -> dict:
    if changed_flags is None:
        changed_flags = [True] * steps
    return {
        "ok": True,
        "status": "contact_span_observed",
        "verification_level": "settled_visible_span_with_restoration",
        "background_mode": "minimized",
        "origin": "observed_top",
        "views": [
            {"offset": 0, "rows": [row("Alpha"), row("Alpha")],
             "exposed_count": 2, "returned_count": 2, "viewport_changed": False},
            *[
                {"offset": offset, "rows": [row("Alpha"), row("Beta")][:limit],
                 "exposed_count": 2, "returned_count": min(2, limit),
                 "viewport_changed": changed_flags[offset - 1]}
                for offset in range(1, steps + 1)
            ],
        ],
        "counts": {
            "requested_steps": steps,
            "completed_steps": steps,
            "view_count": steps + 1,
            "changed_view_count": sum(changed_flags),
        },
        "not_full_directory": True,
        "stable_cursor_supported": False,
        "original_contacts_view_restored": True,
        "original_conversation_restored": True,
    }


class ContactSpanContractTests(unittest.TestCase):
    def test_evidence_is_fixed_and_strictly_typed(self):
        fixed, valid = contract.normalize_contact_span_evidence(evidence())
        self.assertTrue(valid)
        self.assertEqual(fixed, evidence())

        missing = dict(evidence())
        del missing["entered"]
        fixed, valid = contract.normalize_contact_span_evidence(missing)
        self.assertFalse(valid)
        self.assertEqual(fixed["primary_error_code"], "contact_span_evidence_invalid")
        self.assertNotIn("entered", fixed)

        extra = {**evidence(), "private_labels": ["must not leak"]}
        fixed, valid = contract.normalize_contact_span_evidence(extra)
        self.assertFalse(valid)
        self.assertNotIn("private_labels", fixed)

        bool_steps = {**evidence(), "completed_steps": True}
        fixed, valid = contract.normalize_contact_span_evidence(bool_steps)
        self.assertFalse(valid)
        self.assertIsNone(fixed["completed_steps"])

        completed_without_view = evidence()
        completed_without_view["observed_view_count"] = 0
        fixed, valid = contract.normalize_contact_span_evidence(completed_without_view)
        self.assertFalse(valid)
        self.assertEqual(fixed["primary_error_code"], "contact_span_evidence_invalid")

        preentry_counts = evidence()
        preentry_counts.update(
            entered=False,
            attempted_down_steps=2,
            completed_steps=2,
            observed_view_count=3,
            attempted_restore_steps=2,
        )
        fixed, valid = contract.normalize_contact_span_evidence(preentry_counts)
        self.assertFalse(valid)
        self.assertEqual(fixed["primary_error_code"], "contact_span_evidence_invalid")

        valid_preentry_failure = evidence()
        valid_preentry_failure.update(
            entered=False,
            attempted_down_steps=0,
            completed_steps=0,
            observed_view_count=0,
            attempted_restore_steps=0,
            viewport_settled=False,
            contacts_view_restored=False,
            conversation_restored=False,
            primary_error_code="contacts_not_opened",
        )
        fixed, valid = contract.normalize_contact_span_evidence(valid_preentry_failure)
        self.assertTrue(valid)
        self.assertEqual(fixed["primary_error_code"], "contacts_not_opened")

        valid_down_failure = evidence()
        valid_down_failure.update(
            entered=True,
            attempted_down_steps=1,
            completed_steps=0,
            observed_view_count=1,
            attempted_restore_steps=None,
            viewport_settled=False,
            contacts_view_restored=None,
            conversation_restored=None,
            primary_error_code="contacts_view_not_settled",
        )
        fixed, valid = contract.normalize_contact_span_evidence(valid_down_failure)
        self.assertTrue(valid)
        self.assertEqual(fixed["attempted_down_steps"], 1)
        self.assertEqual(fixed["completed_steps"], 0)
        self.assertEqual(fixed["observed_view_count"], 1)
        self.assertIsNone(fixed["attempted_restore_steps"])

        foreign = {**preentry_counts, "private_trace": "secret", "primary_error_code": "bad\x00detail"}
        fixed, valid = contract.normalize_contact_span_evidence(foreign)
        self.assertFalse(valid)
        self.assertEqual(fixed["primary_error_code"], "contact_span_failed")
        self.assertNotIn("private_trace", fixed)
        self.assertNotIn("secret", repr(fixed))

    def test_success_requires_counts_offsets_rows_and_independent_desktop_gate(self):
        self.assertTrue(contract.valid_contact_span_success(
            body(), evidence(), {"background_observation_passed": True},
            {"steps": 2, "limit_per_view": 2},
        ))

        for args in (
            {"steps": True, "limit_per_view": 2},
            {"steps": 0, "limit_per_view": 2},
            {"steps": 2, "limit_per_view": 101},
            {"steps": 2, "limit_per_view": 2, "private": "x"},
        ):
            with self.subTest(args=args):
                self.assertFalse(contract.valid_contact_span_success(
                    body(), evidence(), {"background_observation_passed": True}, args,
                ))

        self.assertFalse(contract.valid_contact_span_success(
            body(), evidence(), {"background_observation_passed": False},
            {"steps": 2, "limit_per_view": 2},
        ))

        changed_origin = body()
        changed_origin["views"][0]["viewport_changed"] = True
        self.assertFalse(contract.valid_contact_span_success(
            changed_origin, evidence(), {"background_observation_passed": True},
            {"steps": 2, "limit_per_view": 2},
        ))

        for flags in ([False, False], [True, False]):
            with self.subTest(changed_flags=flags):
                self.assertTrue(contract.valid_contact_span_success(
                    body(steps=2, limit=2, changed_flags=flags), evidence(),
                    {"background_observation_passed": True},
                    {"steps": 2, "limit_per_view": 2},
                ))

    def test_body_rejects_private_fields_nul_bad_counts_and_bad_restoration(self):
        cases = []
        extra = body()
        extra["views"][0]["contact_ref"] = "private-ref"
        cases.append(extra)
        nul = body()
        nul["views"][1]["rows"][0]["display_text"] = "A\x00B"
        cases.append(nul)
        bad_count = body()
        bad_count["counts"]["view_count"] = 2
        cases.append(bad_count)
        bool_offset = body()
        bool_offset["views"][1]["offset"] = True
        cases.append(bool_offset)
        wrong_restore = body()
        wrong_restore["original_conversation_restored"] = False
        cases.append(wrong_restore)
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(contract.valid_contact_span_success(
                    value, evidence(), {"background_observation_passed": True},
                    {"steps": 2, "limit_per_view": 2},
                ))

    def test_duplicates_are_kept_and_journal_summary_has_no_rows_or_labels(self):
        value = body(steps=1, limit=2)
        self.assertEqual([r["display_text"] for r in value["views"][0]["rows"]], ["Alpha", "Alpha"])
        summary = contract.contact_span_journal_summary(value)
        self.assertEqual(summary["counts"], value["counts"])
        self.assertNotIn("views", summary)
        self.assertNotIn("rows", summary)
        self.assertNotIn("Alpha", repr(summary))
        self.assertNotIn("display_text", repr(summary))

        invalid = body()
        invalid["views"][0]["rows"][0]["hidden"] = "x"
        with self.assertRaisesRegex(ValueError, "contact_span_result_invalid"):
            contract.contact_span_journal_summary(invalid)

    def test_safe_error_allowlist_never_returns_unknown_text(self):
        self.assertEqual(contract.safe_contact_span_error("contacts_top_required"), "contacts_top_required")
        for code in (
            "unverified_geometry",
            "invalid_contact_span_steps", "invalid_contact_span_limit",
            "invalid_deadline", "contact_span_budget_exhausted",
            "contact_span_already_started", "contact_span_row_invalid",
            "background_side_effect", "monitor_failed",
        ):
            with self.subTest(code=code):
                self.assertEqual(contract.safe_contact_span_error(code), code)
        self.assertEqual(contract.safe_contact_span_error("private exception detail"), "contact_span_failed")
        self.assertEqual(contract.safe_contact_span_error(None), "contact_span_failed")

        for code in ("invalid_contact_span_steps", "contact_span_row_invalid"):
            value = evidence()
            value["primary_error_code"] = code
            fixed, valid = contract.normalize_contact_span_evidence(value)
            self.assertTrue(valid)
            self.assertEqual(fixed["primary_error_code"], code)

        nul_code = evidence()
        nul_code["primary_error_code"] = "contact_span_row_invalid\x00detail"
        fixed, valid = contract.normalize_contact_span_evidence(nul_code)
        self.assertFalse(valid)
        self.assertEqual(fixed["primary_error_code"], "contact_span_failed")
        self.assertNotEqual(fixed["primary_error_code"], "contact_span_evidence_invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
