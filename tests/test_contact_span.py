"""Owned staged fakes for bounded contact-span collection; never touch Weixin."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for path in (str(SRC), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    from wxbg import contact_span, contact_scroll, contact_span_contract
    from wxbg.policy import AdapterError
except ImportError:
    contact_span = None
    contact_scroll = None
    contact_span_contract = None
    AdapterError = None

from test_contact_actions import FakeAdapter, Node  # noqa: E402


class SpanAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.offset = 0
        self.wheels = []
        self.fail_down = False
        self.fail_up = False
        self.change_on_restore = False
        self.arm_change_on_restore = False
        self.unknown_origin = False

    def nodes(self):
        if self.mode == "contacts":
            top = 201 if self.unknown_origin else 200
            if self.offset == 0:
                self.extra_contacts = [Node(
                    "mmui::ContactsCellMangerBtnView", "Owned manager", "manager",
                    bounds=(150, top, 667, top + 130), parent=self.table,
                )]
                names = [("Alpha", "row-1", 870), ("Beta", "row-2", 1005), ("Alpha", "row-3", 1140)]
            elif self.offset == 1:
                self.extra_contacts = []
                names = [("Same label", "scroll-1", 125), ("Same label", "scroll-2", 1905),
                         ("New contact", "scroll-3", 2010)]
            else:
                self.extra_contacts = []
                names = [("Delta", "destination-1", 260), ("Same label", "destination-2", 395)]
            if self.change_on_restore and self.offset == 0:
                names[0] = ("Changed private label", names[0][1], names[0][2])
            self.rows = [self.row(name, rid, top) for name, rid, top in names]
        return super().nodes()

    def wheel(self, adapter, table, delta):
        self.wheels.append(delta)
        if delta < 0:
            self.offset += 1
            if self.arm_change_on_restore:
                self.change_on_restore = True
            if self.fail_down:
                raise AdapterError("wheel_result_unknown")
        else:
            self.offset = max(0, self.offset - 1)
            if self.fail_up:
                raise AdapterError("wheel_result_unknown")


def collect(adapter, **kwargs):
    return contact_span.collect_contact_span(
        adapter,
        deadline=time.monotonic() + 30.0,
        wheel=adapter.wheel,
        sleep=lambda _: None,
        **kwargs,
    )


class ContactSpanTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(contact_span, "staged collector is not implemented")
        self.adapter = SpanAdapter()
        self.sleep = patch.object(contact_scroll.time, "sleep", return_value=None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_current_measured_sidebar_tabs_allow_entry_and_chat_restore(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        # The Contacts table is awaiting its separate live measurement.
        with patch.object(contact_scroll, '_snapshot', side_effect=AdapterError('contacts_page_changed')):
            with self.assertRaises(AdapterError) as caught:
                collect(self.adapter, steps=1, limit_per_view=100)
        self.assertEqual(caught.exception.code, 'contacts_page_changed')
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])
        self.assertTrue(self.adapter.contact_span_evidence['entered'])
        self.assertTrue(self.adapter.contact_span_evidence['conversation_restored'])

    def test_current_measured_contacts_layout_collects_and_restores_span(self):
        self.adapter.root_node.bounds = (2, 0, 3237, 1995)
        self.adapter.wechat.bounds = (2, 240, 152, 330)
        self.adapter.contacts.bounds = (2, 360, 152, 450)
        self.adapter.table.bounds = (152, 200, 669, 1985)
        original_nodes = self.adapter.nodes

        def current_nodes():
            nodes = original_nodes()
            if self.adapter.mode == 'contacts':
                for node in nodes:
                    if node.element_info.class_name.startswith('mmui::ContactsCell'):
                        _, top, _, bottom = node.bounds
                        if top >= 1985:
                            bottom, top = 1900 + bottom - top, 1900
                        node.bounds = (152, top, 669, bottom)
            return nodes

        self.adapter.nodes = current_nodes
        result = collect(self.adapter, steps=1, limit_per_view=100)
        self.assertTrue(result['original_contacts_view_restored'])
        self.assertTrue(result['original_conversation_restored'])
        self.assertEqual(self.adapter.wheels, [-120, 120])
        self.assertEqual(self.adapter.clicks, ['通訊錄', '微信'])

    def test_phase_marks_bracket_reads_and_both_restorations(self):
        observed = []
        result = collect(self.adapter, steps=2, limit_per_view=100,
            phase=lambda name: observed.append((name, self.adapter.mode, self.adapter.offset)))
        self.assertTrue(result["ok"])
        self.assertEqual(observed, [
            ("enter_contacts", "chat", 0), ("span_read", "contacts", 0),
            ("restore_contacts", "contacts", 2), ("restore_chat", "contacts", 0),
            ("complete", "chat", 0),
        ])

    def test_phase_callback_failure_keeps_cleanup_and_withholds_success(self):
        for failed_phase in ("enter_contacts", "span_read", "restore_contacts", "restore_chat", "complete"):
            with self.subTest(phase=failed_phase):
                adapter = SpanAdapter()
                def phase(name):
                    if name == failed_phase:
                        raise RuntimeError("private phase diagnostic text")
                with self.assertRaises(AdapterError) as caught:
                    collect(adapter, steps=1, limit_per_view=100, phase=phase)
                self.assertEqual(caught.exception.code, "monitor_failed")
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual((adapter.mode, adapter.offset, adapter.text), ("chat", 0, ""))
                self.assertEqual(adapter.wheels, [-120, 120])

    def test_failure_never_marks_phase_complete(self):
        observed = []
        self.adapter.fail_down = True
        with self.assertRaises(AdapterError):
            collect(self.adapter, steps=1, limit_per_view=100, phase=observed.append)
        self.assertEqual(observed, ["enter_contacts", "span_read", "restore_contacts", "restore_chat"])

    def test_reads_origin_and_each_destination_preserving_duplicate_labels_then_restores_both_views(self):
        result = collect(self.adapter, steps=2, limit_per_view=100)

        self.assertEqual(self.adapter.wheels, [-120, -120, 120, 120])
        self.assertEqual((self.adapter.offset, self.adapter.mode, self.adapter.text), (0, "chat", ""))
        self.assertEqual([row["display_text"] for row in result["views"][0]["rows"]], ["Alpha", "Beta", "Alpha"])
        self.assertEqual([row["display_text"] for row in result["views"][1]["rows"]],
                         ["Same label", "Same label", "New contact"])
        self.assertEqual(result["counts"], {
            "requested_steps": 2, "completed_steps": 2, "view_count": 3, "changed_view_count": 2,
        })
        self.assertEqual(set(result), {
            "ok", "status", "verification_level", "background_mode", "origin", "views", "counts",
            "not_full_directory", "stable_cursor_supported", "original_contacts_view_restored",
            "original_conversation_restored",
        })
        self.assertTrue(result["original_contacts_view_restored"])
        self.assertTrue(result["original_conversation_restored"])
        self.assertEqual(set(self.adapter.contact_span_evidence), {
            "mode", "origin", "requested_steps", "delivery_started", "entered",
            "attempted_down_steps", "completed_steps", "observed_view_count",
            "attempted_restore_steps", "viewport_settled", "contacts_view_restored",
            "conversation_restored", "draft_preserved", "not_full_directory",
            "stable_cursor_supported", "primary_error_code", "cleanup_error_code",
        })

    def test_real_collector_output_passes_actual_contract_and_journal_summary_is_metadata_only(self):
        result = collect(self.adapter, steps=1, limit_per_view=100)

        self.assertTrue(contact_span_contract.valid_contact_span_success(
            result,
            self.adapter.contact_span_evidence,
            {"background_observation_passed": True},
            {"steps": 1, "limit_per_view": 100},
        ))
        summary = contact_span_contract.contact_span_journal_summary(result)
        self.assertNotIn("views", summary)
        self.assertNotIn("Alpha", repr(summary))
        self.assertEqual(summary["counts"]["view_count"], 2)

    def test_destination_view_may_be_unchanged_without_boundary_claim(self):
        def no_op_wheel(adapter, table, delta):
            self.adapter.wheels.append(delta)

        result = contact_span.collect_contact_span(
            self.adapter,
            steps=1,
            limit_per_view=100,
            deadline=time.monotonic() + 30.0,
            wheel=no_op_wheel,
            sleep=lambda _: None,
        )

        self.assertEqual(result["counts"]["changed_view_count"], 0)
        self.assertFalse(result["views"][1]["viewport_changed"])
        self.assertTrue(result["not_full_directory"])
        self.assertFalse(result["stable_cursor_supported"])

    def test_requires_observed_top_and_does_not_seek_before_first_wheel(self):
        self.adapter.unknown_origin = True

        with self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=1, limit_per_view=100)

        self.assertEqual(caught.exception.code, "contacts_top_required")
        self.assertEqual(self.adapter.wheels, [])
        self.assertEqual(self.adapter.mode, "chat")
        self.assertEqual(self.adapter.contact_span_evidence["primary_error_code"], "contacts_top_required")
        self.assertEqual(self.adapter.contact_span_evidence["attempted_down_steps"], 0)

    def test_first_down_failure_stops_forward_progress_and_attempts_one_inverse(self):
        self.adapter.fail_down = True

        with self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=3, limit_per_view=100)

        self.assertEqual(caught.exception.code, "wheel_result_unknown")
        self.assertEqual(self.adapter.wheels, [-120, 120])
        self.assertEqual(self.adapter.contact_span_evidence["attempted_down_steps"], 1)
        self.assertEqual(self.adapter.contact_span_evidence["completed_steps"], 0)
        self.assertEqual(self.adapter.contact_span_evidence["observed_view_count"], 1)
        self.assertEqual(self.adapter.contact_span_evidence["attempted_restore_steps"], 1)
        self.assertTrue(self.adapter.contact_span_evidence["delivery_started"])
        self.assertTrue(self.adapter.contact_span_evidence["conversation_restored"])

    def test_inverse_failure_is_preserved_and_chat_restore_is_still_attempted(self):
        self.adapter.fail_up = True

        with self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=1, limit_per_view=100)

        self.assertEqual(caught.exception.code, "contacts_scroll_restore_failed")
        self.assertEqual(self.adapter.wheels, [-120, 120])
        self.assertFalse(self.adapter.contact_span_evidence["contacts_view_restored"])
        self.assertTrue(self.adapter.contact_span_evidence["conversation_restored"])
        self.assertEqual(self.adapter.contact_span_evidence["cleanup_error_code"], "wheel_result_unknown")

    def test_changed_origin_signature_fails_restoration_without_returning_private_label(self):
        self.adapter.arm_change_on_restore = True

        with self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=1, limit_per_view=100)

        self.assertEqual(caught.exception.code, "contacts_scroll_restore_failed")
        self.assertNotIn("Changed private label", repr(caught.exception))
        self.assertFalse(self.adapter.contact_span_evidence["contacts_view_restored"])
        self.assertTrue(self.adapter.contact_span_evidence["conversation_restored"])

    def test_invalid_bounds_and_nonfinite_deadline_never_click_or_wheel(self):
        for kwargs in (
            {"steps": 0, "limit_per_view": 100},
            {"steps": 5, "limit_per_view": 100},
            {"steps": True, "limit_per_view": 100},
            {"steps": 1, "limit_per_view": 0},
            {"steps": 1, "limit_per_view": 101},
        ):
            with self.subTest(kwargs=kwargs):
                adapter = SpanAdapter()
                with self.assertRaises(AdapterError):
                    contact_span.collect_contact_span(
                        adapter, deadline=time.monotonic() + 30.0, wheel=adapter.wheel,
                        sleep=lambda _: None, **kwargs,
                    )
                self.assertEqual((adapter.clicks, adapter.wheels), ([], []))

        for deadline in (float("nan"), float("inf"), float("-inf")):
            adapter = SpanAdapter()
            with self.subTest(deadline=deadline), self.assertRaises(AdapterError) as caught:
                contact_span.collect_contact_span(
                    adapter, steps=1, limit_per_view=100, deadline=deadline,
                    wheel=adapter.wheel, sleep=lambda _: None,
                )
            self.assertEqual(caught.exception.code, "invalid_deadline")
            self.assertEqual((adapter.clicks, adapter.wheels), ([], []))

    def test_cleanup_reserve_budget_rejects_before_navigation_and_records_fixed_error(self):
        deadline = time.monotonic() + 0.01

        with self.assertRaises(AdapterError) as caught:
            contact_span.collect_contact_span(
                self.adapter, steps=4, limit_per_view=100, deadline=deadline,
                wheel=self.adapter.wheel, sleep=lambda _: None,
            )

        self.assertEqual(caught.exception.code, "contact_span_budget_exhausted")
        self.assertEqual(self.adapter.contact_span_evidence["primary_error_code"],
                         "contact_span_budget_exhausted")
        self.assertEqual((self.adapter.clicks, self.adapter.wheels), ([], []))

    def test_already_started_rejects_without_navigation(self):
        self.adapter.contact_span_started = True

        with self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=1, limit_per_view=100)

        self.assertEqual(caught.exception.code, "contact_span_already_started")
        self.assertTrue(self.adapter.contact_span_evidence["delivery_started"])
        self.assertEqual((self.adapter.clicks, self.adapter.wheels), ([], []))

    def test_malformed_second_call_retains_started_evidence_and_unknown_state(self):
        collect(self.adapter, steps=1, limit_per_view=100)
        prior = dict(self.adapter.contact_span_evidence)

        with self.assertRaises(AdapterError) as caught:
            contact_span.collect_contact_span(
                self.adapter,
                steps=0,
                limit_per_view=101,
                deadline=float("nan"),
                wheel=self.adapter.wheel,
                sleep=lambda _: None,
            )

        self.assertEqual(caught.exception.code, "contact_span_already_started")
        self.assertTrue(getattr(caught.exception, "outcome_unknown"))
        self.assertEqual(self.adapter.contact_span_evidence["requested_steps"], prior["requested_steps"])
        self.assertEqual(self.adapter.contact_span_evidence["attempted_down_steps"],
                         prior["attempted_down_steps"])
        self.assertTrue(self.adapter.contact_span_evidence["delivery_started"])
        self.assertEqual(self.adapter.wheels, [-120, 120])

    def test_unsettled_destination_is_not_counted_as_completed(self):
        original_settled = contact_span.cs._settled
        calls = 0

        def fail_destination(guard, table_id, initial=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AdapterError("contacts_view_not_settled")
            return original_settled(guard, table_id, initial)

        with patch.object(contact_span.cs, "_settled", side_effect=fail_destination), self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=1, limit_per_view=100)

        self.assertEqual(caught.exception.code, "contacts_view_not_settled")
        self.assertEqual(self.adapter.contact_span_evidence["attempted_down_steps"], 1)
        self.assertEqual(self.adapter.contact_span_evidence["completed_steps"], 0)
        self.assertEqual(self.adapter.contact_span_evidence["observed_view_count"], 1)

    def test_cleanup_budget_uses_only_remaining_inverse_steps_near_boundary(self):
        base = time.monotonic()
        deadline = base + 30.0
        state = {"now": base, "settled_reads": 0, "cleanup_started": False}
        original_settled = contact_span.cs._settled

        def guard_clock():
            return state["now"]

        def budget_clock():
            # Enter the cleanup boundary only after the final read has settled.
            # All guards, wheels and waits use the same deterministic clock;
            # the test does not rely on finishing real Python work in 10 ms.
            if state["settled_reads"] == 3 and not state["cleanup_started"]:
                state["now"] = deadline - (
                    contact_span.OUTER_RESPONSE_RESERVE_SECONDS
                    + contact_span.CHAT_CLEANUP_RESERVE_SECONDS
                    + contact_span._cleanup_reserve(2)
                    + 0.01
                )
                state["cleanup_started"] = True
            return state["now"]

        def settled(guard, table_id, initial=None):
            value = original_settled(guard, table_id, initial)
            state["settled_reads"] += 1
            return value

        def timed_wheel(adapter, table, delta):
            self.adapter.wheel(adapter, table, delta)
            state["now"] += contact_span.WHEEL_DELIVERY_RESERVE_SECONDS

        def timed_sleep(seconds):
            state["now"] += seconds

        with patch.object(contact_span.ca.time, "monotonic", side_effect=guard_clock), \
                patch.object(contact_span.cs.time, "sleep", side_effect=timed_sleep), \
                patch.object(contact_span.cs, "_settled", side_effect=settled):
            result = contact_span.collect_contact_span(
                self.adapter,
                steps=2,
                limit_per_view=100,
                deadline=deadline,
                wheel=timed_wheel,
                clock=budget_clock,
                sleep=timed_sleep,
            )

        self.assertEqual(result["counts"]["completed_steps"], 2)
        self.assertEqual(self.adapter.wheels, [-120, -120, 120, 120])
        self.assertTrue(result["original_contacts_view_restored"])
        self.assertTrue(state["cleanup_started"])
        self.assertLess(state["now"], deadline - contact_span.OUTER_RESPONSE_RESERVE_SECONDS)

    def test_malformed_snapshot_row_uses_fixed_contact_span_row_error(self):
        original_settled = contact_span.cs._settled

        def malformed(guard, table_id, initial=None):
            view = original_settled(guard, table_id, initial)
            view["contacts"] = [{"private": "must not escape"}]
            return view

        with patch.object(contact_span.cs, "_settled", side_effect=malformed), self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=1, limit_per_view=100)

        self.assertEqual(caught.exception.code, "contact_span_row_invalid")
        self.assertNotIn("must not escape", repr(caught.exception))
        self.assertTrue(self.adapter.contact_span_evidence["conversation_restored"])

    def test_uncertain_contacts_tab_activation_is_delivery_started_and_restores_chat(self):
        def fail_contacts(node):
            if node is self.adapter.contacts:
                raise AdapterError("contacts_click_failed")

        self.adapter.on_click = fail_contacts

        with self.assertRaises(AdapterError) as caught:
            collect(self.adapter, steps=1, limit_per_view=100)

        self.assertEqual(caught.exception.code, "contacts_click_failed")
        self.assertTrue(self.adapter.contact_span_evidence["delivery_started"])
        self.assertFalse(self.adapter.contact_span_evidence["entered"])
        self.assertTrue(self.adapter.contact_span_evidence["conversation_restored"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
