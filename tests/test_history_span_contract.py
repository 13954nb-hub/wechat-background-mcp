import unittest

from wxbg import history_span_contract as contract


def row(text, kind="mmui::ChatTextItemView"):
    return {"kind": kind, "text": text, "bounds": [667, 105, 3229, 245]}


def body(steps=1, text="private span text"):
    return {
        "ok": True,
        "status": "history_span_observed",
        "verification_level": "settled_visible_span_with_final_destination_retained",
        "background_mode": "minimized",
        "direction": "older",
        "views": [
            {"offset": 0, "rows": [row(text)], "exposed_count": 1,
             "root_bounds": [0, 0, 3240, 2040],
             "viewport_bounds": [667, 200, 3229, 1680],
             "returned_count": 1, "viewport_changed": False},
            *[
                {"offset": index, "rows": [row(text)], "exposed_count": 1,
                 "root_bounds": [0, 0, 3240, 2040],
                 "viewport_bounds": [667, 200, 3229, 1680],
                 "returned_count": 1, "viewport_changed": True}
                for index in range(1, steps + 1)
            ],
        ],
        "counts": {
            "requested_steps": steps,
            "completed_steps": steps,
            "view_count": steps + 1,
            "changed_view_count": steps,
        },
        "refs": {"conversation": "session-opaque-ref"},
        "not_full_history": True,
        "boundary_verified": False,
        "chronological_order_verified": False,
        "final_view_retained": True,
    }


def evidence(steps=1):
    return {
        "mode": "intentional_navigation_span",
        "direction": "older",
        "requested_steps": steps,
        "delivery_started": True,
        "completed_steps": steps,
        "observed_view_count": steps + 1,
        "viewport_settled": True,
        "conversation_preserved": True,
        "draft_preserved": True,
        "final_view_retained": True,
        "not_full_history": True,
        "boundary_verified": False,
        "chronological_order_verified": False,
        "primary_error_code": None,
    }


class HistorySpanContractTests(unittest.TestCase):
    def test_new_window_geometry_is_bound_to_each_row_and_each_view(self):
        dynamic = body()
        for view in dynamic['views']:
            view['root_bounds'] = [300, 80, 1900, 1080]
            view['viewport_bounds'] = [680, 200, 1800, 800]
            view['rows'][0]['bounds'] = [680, 105, 1800, 245]
        args = {"session_ref": "session-opaque-ref", "direction": "older",
                "steps": 1, "limit_per_view": 200}
        self.assertTrue(contract.valid_history_span_success(
            dynamic, evidence(), {"background_observation_passed": True}, args))
        dynamic['views'][1]['viewport_bounds'][2] = 1799
        self.assertFalse(contract.valid_history_span_success(
            dynamic, evidence(), {"background_observation_passed": True}, args))

    def test_normalizer_is_typed_and_rejects_missing_extra_and_bool_integer_fields(self):
        fixed, valid = contract.normalize_history_span_evidence(evidence())
        self.assertTrue(valid)
        self.assertEqual(fixed, evidence())

        missing = dict(evidence()); del missing["final_view_retained"]
        fixed, valid = contract.normalize_history_span_evidence(missing)
        self.assertFalse(valid)
        self.assertEqual(fixed["primary_error_code"], "history_span_evidence_invalid")

        extra = {**evidence(), "private_text": "must not leak"}
        fixed, valid = contract.normalize_history_span_evidence(extra)
        self.assertFalse(valid)
        self.assertNotIn("private_text", fixed)

        bool_count = {**evidence(), "completed_steps": True}
        fixed, valid = contract.normalize_history_span_evidence(bool_count)
        self.assertFalse(valid)
        self.assertIsNone(fixed["completed_steps"])

    def test_failure_can_record_wheel_delivered_before_destination_observation(self):
        progress = {
            **evidence(steps=4),
            "completed_steps": 2,
            "observed_view_count": 2,
            "viewport_settled": None,
            "primary_error_code": "history_row_geometry_invalid",
        }
        fixed, valid = contract.normalize_history_span_evidence(progress)
        self.assertTrue(valid)
        self.assertEqual(fixed, progress)

    def test_unobserved_destination_requires_unsettled_failure(self):
        progress = {
            **evidence(steps=4),
            "completed_steps": 2,
            "observed_view_count": 2,
            "viewport_settled": None,
            "primary_error_code": "history_row_geometry_invalid",
        }
        for changed in ({"primary_error_code": None}, {"viewport_settled": True}):
            with self.subTest(changed=changed):
                fixed, valid = contract.normalize_history_span_evidence({
                    **progress, **changed,
                })
                self.assertFalse(valid)
                self.assertEqual(fixed["primary_error_code"],
                                 "history_span_evidence_invalid")

    def test_failure_progress_rejects_impossible_view_counts(self):
        progress = {
            **evidence(steps=4),
            "completed_steps": 2,
            "observed_view_count": 2,
            "viewport_settled": None,
            "primary_error_code": "history_row_geometry_invalid",
        }
        for count in (0, 1, 4, 5):
            with self.subTest(observed_view_count=count):
                fixed, valid = contract.normalize_history_span_evidence({
                    **progress, "observed_view_count": count,
                })
                self.assertFalse(valid)
                self.assertEqual(fixed["primary_error_code"],
                                 "history_span_evidence_invalid")

    def test_success_requires_exact_body_counts_views_and_typed_evidence(self):
        value = body(steps=2)
        self.assertTrue(contract.valid_history_span_success(
            value, evidence(steps=2), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 2, "limit_per_view": 200},
        ))

        changed = body(steps=2)
        changed["counts"]["view_count"] = 1
        self.assertFalse(contract.valid_history_span_success(
            changed, evidence(steps=2), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 2, "limit_per_view": 200},
        ))

        changed = body()
        changed["views"][0]["rows"][0]["runtime_id"] = "private-runtime-id"
        self.assertFalse(contract.valid_history_span_success(
            changed, evidence(), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))

        invented_semantic_kind = body()
        invented_semantic_kind["views"][0]["rows"][0]["kind"] = "message"
        self.assertFalse(contract.valid_history_span_success(
            invented_semantic_kind, evidence(), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))

    def test_producer_geometry_allows_a_partially_clipped_negative_top_row(self):
        clipped = body()
        clipped["views"][0]["rows"][0]["bounds"] = [667, -20, 3229, 245]
        self.assertTrue(contract.valid_history_span_success(
            clipped, evidence(), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))

    def test_current_verified_layout_rows_are_accepted_without_allowing_mixed_frames(self):
        current = body()
        for view in current["views"]:
            view["viewport_bounds"] = [670, 200, 3227, 1680]
            for item in view["rows"]:
                item["bounds"][0] = 670
                item["bounds"][2] = 3227
        args = {"session_ref": "session-opaque-ref", "direction": "older",
                "steps": 1, "limit_per_view": 200}
        self.assertTrue(contract.valid_history_span_success(
            current, evidence(), {"background_observation_passed": True}, args,
        ))
        self.assertEqual(contract.history_span_journal_summary(current)["counts"]["view_count"], 2)

        mixed = body()
        mixed["views"][1]["rows"][0]["bounds"] = [670, 105, 3227, 245]
        self.assertFalse(contract.valid_history_span_success(
            mixed, evidence(), {"background_observation_passed": True}, args,
        ))

    def test_current_direct_image_reference_row_has_closed_public_kind(self):
        current = body()
        for view in current["views"]:
            view["viewport_bounds"] = [670, 200, 3227, 1680]
            view["rows"][0] = {"kind": "mmui::ChatBubbleReferItemView",
                               "text": "圖片", "bounds": [670, 1352, 3227, 1634]}
        args = {"session_ref": "session-opaque-ref", "direction": "older",
                "steps": 1, "limit_per_view": 200}
        self.assertTrue(contract.valid_history_span_success(
            current, evidence(), {"background_observation_passed": True}, args,
        ))
        self.assertEqual(contract.history_span_journal_summary(current)["counts"]["view_count"], 2)

        current["views"][0]["rows"][0]["kind"] = "mmui::ChatUnknownItemView"
        self.assertFalse(contract.valid_history_span_success(
            current, evidence(), {"background_observation_passed": True}, args,
        ))

    def test_success_cross_checks_counts_against_args_and_evidence_and_offsets_are_strict_ints(self):
        value = body(steps=1)
        self.assertFalse(contract.valid_history_span_success(
            value, evidence(steps=1), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 2, "limit_per_view": 200},
        ))
        self.assertFalse(contract.valid_history_span_success(
            value, evidence(steps=2), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))

        bool_offset = body(steps=1)
        bool_offset["views"][0]["offset"] = False
        self.assertFalse(contract.valid_history_span_success(
            bool_offset, evidence(), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))

    def test_origin_cannot_count_as_changed_destination(self):
        changed = body()
        changed["views"][0]["viewport_changed"] = True
        changed["counts"]["changed_view_count"] = 2
        self.assertFalse(contract.valid_history_span_success(
            changed, evidence(), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))

    def test_returned_rows_must_match_exposed_rows_up_to_limit(self):
        missing = body()
        missing["views"][0]["rows"] = []
        missing["views"][0]["returned_count"] = 0
        self.assertFalse(contract.valid_history_span_success(
            missing, evidence(), {"background_observation_passed": True},
            {"session_ref": "session-opaque-ref", "direction": "older",
             "steps": 1, "limit_per_view": 200},
        ))

    def test_journal_summary_contains_metadata_only_and_safe_error_codes_are_closed(self):
        summary = contract.history_span_journal_summary(body())
        self.assertEqual(set(summary), {
            "ok", "status", "verification_level", "counts", "refs", "background_mode",
        })
        self.assertNotIn("views", summary)
        self.assertEqual(contract.safe_history_span_error("context_conflict"), "context_conflict")
        self.assertEqual(contract.safe_history_span_error("private text"), "history_span_failed")

    def test_emitted_history_errors_survive_worker_and_supervisor_normalization(self):
        for code in ("history_view_changed", "tree_budget_exceeded",
                     "ambiguous_recipient", "history_row_identity_invalid",
                     "history_row_geometry_invalid", "cannot_disable_auto_focus",
                     "payment_excluded"):
            with self.subTest(code=code):
                self.assertEqual(contract.safe_history_span_error(code), code)
                fixed, valid = contract.normalize_history_span_evidence({
                    **evidence(), "primary_error_code": code,
                })
                self.assertTrue(valid)
                self.assertEqual(fixed["primary_error_code"], code)
                self.assertNotIn("private span text", str(fixed))


if __name__ == "__main__":
    unittest.main()
