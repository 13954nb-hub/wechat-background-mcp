"""Pure state-machine tests for the private PNG entry investigation."""

from copy import deepcopy
import hashlib
import json
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg import image_entry_probe as probe_module
from wxbg.policy import AdapterError


SESSION_REF = "a" * 32
TITLE = "檔案傳輸"
PNG_DESCRIPTOR = {
    "path": r"C:\owned-image.png",
    "name": "owned-image.png",
    "size": 17,
    "sha256": "1" * 64,
}
ROOT_RECT = (0, 0, 3240, 2040)
ATTACHMENT_RECT = (887, 1920, 957, 1990)
SEND_RECT = (3060, 1922, 3180, 1987)
NEW_ROOT_RECT = (2, 0, 3237, 1995)
NEW_ATTACHMENT_RECT = (890, 1875, 960, 1945)
NEW_SEND_RECT = (3057, 1877, 3177, 1942)


class Rect:
    def __init__(self, bounds):
        self.left, self.top, self.right, self.bottom = bounds


class Info:
    def __init__(self, class_name, control_type, name, automation_id, runtime_id):
        self.class_name = class_name
        self.control_type = control_type
        self.name = name
        self.automation_id = automation_id
        self.runtime_id = runtime_id


class Node:
    def __init__(self, class_name, control_type, name, automation_id, runtime_id,
                 bounds=(100, 100, 300, 200), selected=False, draft=None):
        self.element_info = Info(class_name, control_type, name, automation_id, runtime_id)
        self._rect = Rect(bounds)
        if class_name == "mmui::ChatSessionCell":
            self.iface_selection_item = SimpleNamespace(CurrentIsSelected=selected)
        if class_name == "mmui::ChatInputField":
            self.iface_value = SimpleNamespace(CurrentValue=draft)

    def rectangle(self):
        return self._rect


class FakeDriver:
    def __init__(self, report=None):
        self.report = deepcopy(report or native_report())
        self.select_calls = []
        self.attachment_points = []

    def select_file(self, descriptor, *, attachment_point=(922, 1955), attachment_size=None):
        self.select_calls.append(deepcopy(descriptor))
        self.attachment_points.append(attachment_point)
        return deepcopy(self.report)


def native_report(**changes):
    report = {
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
    report.update(changes)
    return report


def state(*, draft="", messages=(), field_name=TITLE,
          attachment_bounds=ATTACHMENT_RECT, send_bounds=SEND_RECT):
    return {
        "draft": draft,
        "messages": tuple(messages),
        "field_name": field_name,
        "attachment_bounds": attachment_bounds,
        "send_bounds": send_bounds,
    }


def message(kind, runtime, name, bounds=(100, 100, 300, 200)):
    return kind, runtime, name, bounds


def image_message(runtime="image-runtime", name="图片"):
    return message("mmui::ChatBubbleItemView", runtime, name)


def refer_image_message(runtime="refer-image-runtime"):
    return message("mmui::ChatBubbleReferItemView", runtime, "图片")


def file_card_message(runtime="file-runtime"):
    return message(
        "mmui::ChatBubbleItemView", runtime,
        "檔案\nowned-image.png\n1.5K\n微信电脑版",
    )


class FakeAdapter:
    pid = 101
    hwnd = 202
    created = 303.0

    def __init__(self, states, *, root_rect=ROOT_RECT, title=TITLE):
        self.states = list(states)
        self.title = title
        self.root_node = Node("Root", "Pane", "", "root", "root-runtime", root_rect)
        self.node_index = 0
        self.nodes_calls = 0
        self.precondition_calls = 0
        self.open_session_calls = 0
        self.clicks = []
        self.preflight_calls = []
        self.preflight_override = None
        self.submission_started = False
        self.native_evidence = None

    def precondition(self):
        self.precondition_calls += 1

    def nodes(self):
        self.nodes_calls += 1
        current = self.states[min(self.node_index, len(self.states) - 1)]
        self.node_index += 1
        nodes = [
            Node("mmui::ChatSessionCell", "ListItem", self.title, "session_item_" + self.title,
                 "session-runtime", selected=True),
            Node("mmui::ChatInputField", "Edit", current["field_name"], "chat_input_field",
                 "field-runtime", draft=current["draft"]),
            Node("mmui::XButton", "Button", "傳送檔案", "attach", "attach-runtime",
                 current["attachment_bounds"]),
            Node("mmui::XOutlineButton", "Button", "傳送", "send", "send-runtime",
                 current["send_bounds"]),
        ]
        nodes.extend(
            Node(kind, "Text", name if kind == "mmui::ChatBubbleItemView" else name,
                 "message-" + runtime, runtime, bounds)
            for kind, runtime, name, bounds in current["messages"]
        )
        return nodes

    def root(self):
        return self.root_node

    def ref(self, node, context=""):
        if node.element_info.class_name == "mmui::ChatSessionCell":
            return SESSION_REF
        return hashlib.sha256(
            (context + str(node.element_info.runtime_id)).encode("utf-8")
        ).hexdigest()[:32]

    def open_session(self, _session_ref):
        self.open_session_calls += 1
        raise AssertionError("probe must not navigate to satisfy the selected session")

    def click(self, node):
        self.clicks.append(node.element_info.name)

    def preflight_click(self, node):
        self.preflight_calls.append(node)
        if self.preflight_override is not None:
            return (self.preflight_override(node) if callable(self.preflight_override)
                    else self.preflight_override)
        left, top, _, _ = (self.root_node._rect.left, self.root_node._rect.top,
                           self.root_node._rect.right, self.root_node._rect.bottom)
        rect = node.rectangle()
        x = (rect.left + rect.right) // 2 - left
        y = (rect.top + rect.bottom) // 2 - top
        return (y << 16) | x


class RefMutatingAdapter(FakeAdapter):
    def ref(self, node, context=""):
        value = super().ref(node, context)
        if (node.element_info.class_name == "mmui::ChatBubbleItemView"
                and node.element_info.runtime_id == "unstable-runtime"
                and not getattr(node, "_mutated", False)):
            node._mutated = True
            node.element_info.name = "changed after ref"
        return value


class SleepGatedAdapter(FakeAdapter):
    """Keeps post-native UI unchanged until the probe's settle sleep yields."""

    def __init__(self, states):
        super().__init__(states)
        self.released = False

    def release_after_sleep(self):
        self.released = True

    def nodes(self):
        if self.submission_started and not self.released:
            saved_states, saved_index = self.states, self.node_index
            try:
                self.states = [state(messages=(
                    ("mmui::ChatTextItemView", "old-runtime", "private old text",
                     (400, 300, 600, 400)),
                ))]
                self.node_index = 0
                return super().nodes()
            finally:
                self.states, self.node_index = saved_states, saved_index
        return super().nodes()

    def click(self, node):
        super().click(node)
        self.released = False


class ReboundTextAdapter(FakeAdapter):
    """Qt rebuilds one unchanged text row while staging a PNG."""

    def __init__(self):
        super().__init__(initial_states())

    def nodes(self):
        if self.submission_started:
            old_text = message("mmui::ChatTextItemView", "rebuilt-runtime", "private old text",
                               (400, 300, 600, 400))
            current = state(draft="\ufffc", messages=(old_text,))
            if self.clicks:
                current = state(messages=(old_text, image_message("sent-image-runtime")))
            saved_states, saved_index = self.states, self.node_index
            try:
                self.states = [current]
                self.node_index = 0
                return super().nodes()
            finally:
                self.states, self.node_index = saved_states, saved_index
        return super().nodes()


def initial_states(*, draft="", post_selection=None, pre_send=None, final=None,
                   root_rect=ROOT_RECT, field_name=TITLE):
    old = ("mmui::ChatTextItemView", "old-runtime", "private old text", (400, 300, 600, 400))
    return [
        state(field_name=field_name, messages=(old,)),
        state(field_name=field_name, draft=draft, messages=(old,)),
        state(field_name=field_name, draft=draft, messages=(old,)),
        *(post_selection or []),
        *(pre_send or []),
        *(final or []),
    ]


class ImageEntryProbeTests(unittest.TestCase):
    def test_phase_callback_brackets_native_selection_and_send_without_changing_actions(self):
        adapter = FakeAdapter(initial_states(
            post_selection=[state(draft="\ufffc"), state(draft="\ufffc")],
            pre_send=[state(draft="\ufffc")],
            final=[state(messages=(image_message("phase-image"),)),
                   state(messages=(image_message("phase-image"),))],
        ))
        driver = FakeDriver()
        events = []
        adapter.image_phase = events.append
        original_select = driver.select_file
        original_click = adapter.click

        def select(*args, **kwargs):
            events.append("native_call")
            return original_select(*args, **kwargs)

        def click(node):
            events.append("send_click")
            return original_click(node)

        driver.select_file = select
        adapter.click = click
        self.run_probe(adapter, driver)
        self.assertLess(events.index("image_native_select"), events.index("native_call"))
        self.assertLess(events.index("image_send_dispatch"), events.index("send_click"))
        self.assertLess(events.index("send_click"), events.index("image_final_observe"))
        self.assertEqual(events[-1], "complete")
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, ["傳送"])

    def run_probe(self, adapter, driver=None, descriptor=None, deadline=None):
        return probe_module.probe(
            adapter, driver or FakeDriver(), SESSION_REF,
            descriptor or PNG_DESCRIPTOR, deadline=deadline,
        )

    def failure_detail(self, caught):
        text = str(caught.exception)
        self.assertTrue(text.startswith("outcome_unknown: {"), text)
        return json.loads(text.split(": ", 1)[1])

    @staticmethod
    def measured_new_layout(states):
        for current in states:
            current["attachment_bounds"] = NEW_ATTACHMENT_RECT
            current["send_bounds"] = NEW_SEND_RECT
        return states

    def test_measured_new_layout_uses_exact_native_attachment_point(self):
        states = self.measured_new_layout(initial_states(
            post_selection=[state(draft="\ufffc"), state(draft="\ufffc")],
            pre_send=[state(draft="\ufffc")],
            final=[state(messages=(image_message("new-layout-image"),)),
                   state(messages=(image_message("new-layout-image"),))],
        ))
        adapter = FakeAdapter(states, root_rect=NEW_ROOT_RECT)
        driver = FakeDriver()

        result = self.run_probe(adapter, driver)

        self.assertEqual(result["status"], "local_transition_observed")
        self.assertEqual(driver.attachment_points, [(923, 1910)])
        self.assertEqual(adapter.clicks, ["傳送"])
        self.assertGreaterEqual(len(adapter.preflight_calls), 3)

    def test_new_layout_rejects_unmeasured_attachment_bounds_before_native(self):
        states = self.measured_new_layout(initial_states())
        # The prepared observation must match the first measured layout.
        # A later snapshot drift is rejected before the native picker opens.
        states[1]["attachment_bounds"] = (889, 1875, 959, 1945)
        adapter = FakeAdapter(states, root_rect=NEW_ROOT_RECT)
        driver = FakeDriver()

        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)

        self.assertEqual(caught.exception.code, "unverified_layout")
        self.assertFalse(adapter.submission_started)
        self.assertEqual((driver.select_calls, adapter.clicks), ([], []))

    def test_new_layout_rejects_wrong_attachment_preflight_point_before_native(self):
        states = self.measured_new_layout(initial_states())
        adapter = FakeAdapter(states, root_rect=NEW_ROOT_RECT)
        adapter.preflight_override = (1910 << 16) | 922
        driver = FakeDriver()

        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)

        self.assertEqual(caught.exception.code, "unverified_layout")
        self.assertFalse(adapter.submission_started)
        self.assertEqual((driver.select_calls, adapter.clicks), ([], []))

    def test_unknown_root_rejects_before_native(self):
        # A truly undersized client with stale child bounds is not usable.
        adapter = FakeAdapter(initial_states(), root_rect=(3, 0, 503, 300))
        driver = FakeDriver()

        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)

        self.assertEqual(caught.exception.code, "unverified_layout")
        self.assertFalse(adapter.submission_started)
        self.assertEqual((driver.select_calls, adapter.clicks), ([], []))

    def test_new_layout_rejects_wrong_send_preflight_point_without_click(self):
        states = self.measured_new_layout(initial_states(
            post_selection=[state(draft="\ufffc"), state(draft="\ufffc")],
            pre_send=[state(draft="\ufffc")],
        ))
        adapter = FakeAdapter(states, root_rect=NEW_ROOT_RECT)
        adapter.preflight_override = lambda node: (
            ((1909 << 16) | 3114) if node.element_info.name == "傳送"
            else ((1910 << 16) | 923))
        driver = FakeDriver()

        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)

        # Both semantic points are gated before native selection begins.
        self.assertEqual(caught.exception.code, "unverified_layout")
        self.assertFalse(adapter.submission_started)
        self.assertEqual(driver.attachment_points, [])
        self.assertEqual(driver.select_calls, [])
        self.assertEqual(adapter.clicks, [])

    def test_nonempty_initial_draft_refuses_before_native_selection(self):
        adapter = FakeAdapter(initial_states(draft="existing draft"))
        driver = FakeDriver()
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(caught.exception.code, "draft_conflict")
        self.assertEqual(driver.select_calls, [])
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(adapter.open_session_calls, 0)

    def test_embedded_draft_path_clicks_send_once_and_confirms_two_stable_rows(self):
        adapter = FakeAdapter(initial_states(
            post_selection=[state(draft="\ufffc"), state(draft="\ufffc")],
            pre_send=[state(draft="\ufffc")],
            final=[state(messages=(image_message("image-runtime"),)),
                   state(messages=(image_message("image-runtime"),))],
        ))
        driver = FakeDriver()
        result = self.run_probe(adapter, driver)
        self.assertEqual(result["status"], "local_transition_observed")
        self.assertEqual(set(result), {
            "status", "diagnostic_only", "remote_receipt_verified", "counts", "rows",
        })
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["remote_receipt_verified"])
        self.assertEqual(result["counts"], {
            "native_selections": 1, "send_clicks": 1, "new_rows": 1,
        })
        self.assertEqual(result["rows"][0]["label_category"], "image_label")
        self.assertEqual(len(result["rows"][0]["runtime_sha256"]), 64)
        self.assertNotIn("图片", repr(result))
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(driver.attachment_points, [(922, 1955)])
        self.assertEqual(adapter.clicks, ["傳送"])

    def test_unchanged_text_scene_rebuild_does_not_block_staged_image_send(self):
        adapter = ReboundTextAdapter()
        driver = FakeDriver()

        result = self.run_probe(adapter, driver)

        self.assertEqual(result["status"], "local_transition_observed")
        self.assertEqual(result["counts"], {
            "native_selections": 1, "send_clicks": 1, "new_rows": 1,
        })
        self.assertEqual(adapter.clicks, ["傳送"])
        self.assertEqual(len(driver.select_calls), 1)

        first, second = adapter.image_evidence["selection_frames"]
        self.assertEqual(first["diagnostics"], {
            "draft_initial_kind": "embedded",
            "draft_first_recheck_kind": "embedded",
            "scene_rebind_enabled": True,
            "same_scene_content": True,
            "same_scene_geometry": True,
            "same_scene_runtime": False,
            "new_row_kind_counts": {"text": 0, "bubble": 0,
                                    "bubble_refer": 0, "separator": 0, "other": 0},
            "new_row_label_counts": {"image_label": 0,
                                     "owned_file_card": 0, "other": 0},
            "decision": "continue",
        })
        self.assertEqual(second["diagnostics"]["decision"], "presend")

    def test_unrelated_text_row_exhaustion_records_only_safe_selection_facts(self):
        unrelated = message("mmui::ChatTextItemView", "new-runtime",
                            "private unrelated text", (400, 300, 600, 400))
        adapter = FakeAdapter(initial_states(post_selection=[
            state(draft="\ufffc", messages=(unrelated,)) for _ in range(4)
        ]))
        driver = FakeDriver()

        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)

        self.assertEqual(self.failure_detail(caught), {
            "phase": "selection_draft", "reason_code": "image_entry_observation_unstable",
            "native_selections": 1, "send_calls": 0,
        })
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(len(adapter.image_evidence["selection_frames"]), 4)
        first = adapter.image_evidence["selection_frames"][0]["diagnostics"]
        last = adapter.image_evidence["selection_frames"][-1]["diagnostics"]
        self.assertFalse(first["same_scene_content"])
        self.assertTrue(first["same_scene_geometry"])
        self.assertEqual(first["new_row_kind_counts"]["text"], 1)
        self.assertEqual(first["new_row_label_counts"]["other"], 1)
        self.assertEqual(last["decision"], "max_frames_exhausted")
        self.assertNotIn("private unrelated text", repr(adapter.image_evidence))

    def test_unrelated_automatic_chat_row_is_unknown_without_a_send_click(self):
        automatic = message("mmui::ChatTextItemView", "automatic-runtime", "private caption")
        adapter = FakeAdapter(initial_states(
            post_selection=[state(messages=(automatic,)), state(messages=(automatic,))],
        ))
        driver = FakeDriver()
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(self.failure_detail(caught)['reason_code'],'image_transition_row_unproved')
        self.assertNotIn('private caption',str(caught.exception))
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(len(driver.select_calls), 1)

    def test_chat_item_view_grammar_allows_empty_middle(self):
        self.assertIsNotNone(
            probe_module.ROW_KIND_RE.fullmatch("mmui::ChatItemView")
        )

    def test_new_chat_item_and_refer_image_rows_are_stable_automatic_transition(self):
        rows = (
            message("mmui::ChatItemView", "chat-item-runtime", "五字label"),
            refer_image_message(),
        )
        adapter = FakeAdapter(initial_states(
            post_selection=[state(messages=rows), state(messages=rows)],
        ))
        driver = FakeDriver()
        result = self.run_probe(adapter, driver)
        self.assertEqual(result, {
            "status": "automatic_transition_observed",
            "diagnostic_only": True,
            "remote_receipt_verified": False,
            "counts": {"native_selections": 1, "send_clicks": 0, "new_rows": 2},
            "rows": unittest.mock.ANY,
        })
        self.assertEqual(
            {row["kind"] for row in result["rows"]},
            {"mmui::ChatItemView", "mmui::ChatBubbleReferItemView"},
        )
        self.assertEqual(result["counts"], {
            "native_selections": 1, "send_clicks": 0, "new_rows": 2,
        })
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, [])

    def test_single_chat_item_view_that_disappears_never_sends(self):
        adapter = FakeAdapter(initial_states(
            post_selection=[
                state(messages=(message("mmui::ChatItemView", "vanishing-runtime", "五字label"),)),
                state(draft="\ufffc"),
                state(draft="\ufffc"),
            ],
        ))
        driver = FakeDriver()
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, [])

    def test_new_row_that_disappears_never_sends_or_reselects(self):
        adapter = FakeAdapter(initial_states(
            post_selection=[
                state(messages=(image_message("transient-runtime"),)),
                state(draft="\ufffc"),
                state(draft="\ufffc"),
            ],
        ))
        driver = FakeDriver()
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, [])

    def test_row_identity_snapshot_change_is_unknown_without_send(self):
        adapter = RefMutatingAdapter(initial_states(
            post_selection=[state(messages=(image_message("unstable-runtime"),))],
        ))
        driver = FakeDriver()
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, [])

    def test_changed_context_or_draft_before_send_never_clicks(self):
        adapter = FakeAdapter(initial_states(
            post_selection=[state(draft="\ufffc"), state(draft="changed draft")],
        ))
        driver = FakeDriver()
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, [])

    def test_native_cleanup_failure_is_unknown_without_a_send_click(self):
        adapter = FakeAdapter(initial_states())
        driver = FakeDriver(native_report(released=False, grant_revoked=False))
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(adapter.native_evidence["released"], False)
        self.assertEqual(adapter.clicks, [])
        self.assertEqual(len(driver.select_calls), 1)

    def test_native_report_missing_passed_or_shows_is_unknown(self):
        for missing in ("passed", "shows"):
            adapter = FakeAdapter(initial_states())
            report = native_report()
            report.pop(missing)
            driver = FakeDriver(report)
            with self.assertRaises(AdapterError) as caught:
                self.run_probe(adapter, driver)
            self.assertEqual(caught.exception.code, "outcome_unknown")
            self.assertEqual(len(driver.select_calls), 1)
            self.assertEqual(adapter.clicks, [])

    def test_post_native_generic_error_preserves_evidence(self):
        class EvidenceDriver:
            def select_file(self, _descriptor, *, attachment_point=(922, 1955), attachment_size=None):
                error = RuntimeError("private native detail")
                error.evidence = {"passed": False, "cleanup_errors": []}
                raise error

        adapter = FakeAdapter(initial_states())
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, EvidenceDriver())
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(caught.exception.evidence,
                         {"passed": False, "cleanup_errors": []})
        self.assertEqual(adapter.native_evidence,
                         {"passed": False, "cleanup_errors": []})
        self.assertEqual(adapter.clicks, [])

    def test_settle_sleep_allows_delayed_selection_and_final_rows(self):
        adapter = SleepGatedAdapter(initial_states(
            post_selection=[state(draft="\ufffc"), state(draft="\ufffc")],
            pre_send=[state(draft="\ufffc")],
            final=[state(messages=(image_message("delayed-image"),)),
                   state(messages=(image_message("delayed-image"),))],
        ))
        driver = FakeDriver()
        with patch.object(probe_module.time, "sleep",
                          side_effect=lambda _seconds: adapter.release_after_sleep()) as settle:
            try:
                result = self.run_probe(adapter, driver)
            except AdapterError as error:
                self.fail(f"settle-gated probe did not settle: {error}")
        self.assertEqual(result["status"], "local_transition_observed")
        self.assertGreaterEqual(settle.call_count, 4)
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, ["傳送"])

    def test_deadline_during_settle_does_not_take_another_observation_or_send(self):
        class Clock:
            def __init__(self):
                self.now = 100.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = Clock()
        adapter = FakeAdapter(initial_states(
            post_selection=[state(), state(draft="\ufffc"), state(draft="\ufffc")],
        ))
        driver = FakeDriver()
        with patch.object(probe_module.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(probe_module.time, "sleep", side_effect=clock.sleep), \
             self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver, deadline=100.1)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(adapter.nodes_calls, 4)
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, [])
        detail = self.failure_detail(caught)
        self.assertEqual(detail, {
            "phase": "selection_observe",
            "reason_code": "image_entry_budget_exhausted",
            "native_selections": 1,
            "send_calls": 0,
        })

    def test_native_exception_detail_is_fixed_json_without_raw_text(self):
        class RawNativeDriver:
            def select_file(self, _descriptor, *, attachment_point=(922, 1955), attachment_size=None):
                error = RuntimeError("RAW_NATIVE_SECRET path=C:\\private\\owned.png")
                error.evidence = {"passed": False, "cleanup_errors": []}
                raise error

        adapter = FakeAdapter(initial_states())
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, RawNativeDriver())
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertNotIn("RAW_NATIVE_SECRET", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        detail = self.failure_detail(caught)
        self.assertEqual(set(detail), {
            "phase", "reason_code", "native_selections", "send_calls",
        })
        self.assertEqual(detail["phase"], "native_select")
        self.assertEqual(detail["reason_code"], "unexpected_probe_error")
        self.assertEqual(detail["native_selections"], 1)
        self.assertEqual(detail["send_calls"], 0)

    def test_unallowlisted_adapter_code_is_normalized_without_raw_detail(self):
        class RawAdapterErrorDriver:
            def select_file(self, _descriptor, *, attachment_point=(922, 1955), attachment_size=None):
                raise AdapterError("private_native_code", "RAW_NATIVE_SECRET")

        adapter = FakeAdapter(initial_states())
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, RawAdapterErrorDriver())
        detail = self.failure_detail(caught)
        self.assertEqual(detail["phase"], "native_select")
        self.assertEqual(detail["reason_code"], "unexpected_probe_error")
        self.assertNotIn("private_native_code", str(caught.exception))
        self.assertNotIn("RAW_NATIVE_SECRET", str(caught.exception))

    def test_unstable_final_rows_are_sticky_unknown_without_retry(self):
        final = [
            state(messages=(image_message("image-one"),)),
            state(messages=(image_message("image-two"),)),
            state(messages=(image_message("image-three"),)),
            state(messages=(image_message("image-four"),)),
        ]
        adapter = FakeAdapter(initial_states(
            post_selection=[state(draft="\ufffc"), state(draft="\ufffc")],
            pre_send=[state(draft="\ufffc")],
            final=final,
        ))
        driver = FakeDriver()
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, ["傳送"])

        detail = self.failure_detail(caught)
        self.assertEqual(detail["phase"], "final_draft")
        self.assertEqual(detail["reason_code"], "image_entry_observation_unstable")
        self.assertEqual(detail["native_selections"], 1)
        self.assertEqual(detail["send_calls"], 1)
        with self.assertRaises(AdapterError) as repeated:
            self.run_probe(adapter, driver)
        self.assertEqual(repeated.exception.code, "outcome_unknown")
        previous = self.failure_detail(repeated)
        self.assertEqual(previous, {
            "phase": "previous_attempt",
            "reason_code": "outcome_unknown",
            "native_selections": 0,
            "send_calls": 0,
        })
        self.assertEqual(len(driver.select_calls), 1)
        self.assertEqual(adapter.clicks, ["傳送"])

    def test_file_card_metadata_is_bounded_and_hides_fixture_name(self):
        adapter = FakeAdapter(initial_states(
            post_selection=[state(messages=(file_card_message(),)),
                            state(messages=(file_card_message(),))],
        ))
        result = self.run_probe(adapter, FakeDriver())
        row = result["rows"][0]
        self.assertEqual(result["status"], "automatic_transition_observed")
        self.assertEqual(row["label_category"], "owned_file_card")
        self.assertEqual(row["name_length"], len("檔案\nowned-image.png\n1.5K\n微信电脑版"))
        self.assertEqual(len(row["name_sha256"]), 64)
        self.assertNotIn("owned-image.png", repr(result))

    def test_invalid_png_descriptor_is_rejected_before_ui_or_native(self):
        adapter = FakeAdapter(initial_states())
        driver = FakeDriver()
        invalid = dict(PNG_DESCRIPTOR, path=r"C:\owned-image.jpg", name="owned-image.jpg")
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, driver, invalid)
        self.assertEqual(caught.exception.code, "invalid_image_file_type")
        self.assertEqual(adapter.nodes_calls, 0)
        self.assertEqual(driver.select_calls, [])

    def test_private_probe_defaults_to_file_transfer_title(self):
        generic = "普通聊天"
        adapter = FakeAdapter(
            initial_states(field_name=generic),
            title=generic,
        )
        with self.assertRaises(AdapterError) as caught:
            self.run_probe(adapter, FakeDriver())
        self.assertEqual(caught.exception.code, "context_conflict")
        self.assertEqual(adapter.open_session_calls, 0)

    def test_generic_selected_title_is_allowed_only_in_public_mode(self):
        generic = "普通聊天"
        automatic = image_message("automatic-image-runtime")
        adapter = FakeAdapter(
            initial_states(
                field_name=generic,
                post_selection=[
                    state(field_name=generic, messages=(automatic,)),
                    state(field_name=generic, messages=(automatic,)),
                ],
            ),
            title=generic,
        )
        result = probe_module.probe(
            adapter, FakeDriver(), SESSION_REF, PNG_DESCRIPTOR, own_only=False,
        )
        self.assertEqual(result["status"], "automatic_transition_observed")
        self.assertEqual(adapter.open_session_calls, 0)

    def test_run_requires_the_complete_descriptor_contract(self):
        adapter = SimpleNamespace(pid=101, hwnd=202, created=303.0)
        with self.assertRaises(AdapterError) as caught:
            probe_module.run(adapter, SESSION_REF, PNG_DESCRIPTOR["path"])
        self.assertEqual(caught.exception.code, "invalid_fixture")

    def test_run_holds_verified_descriptor_and_wires_canonical_driver_without_native_call(self):
        descriptor = dict(PNG_DESCRIPTOR)
        verified = []

        class FakeVerifiedFile:
            def __init__(self, path, **kwargs):
                verified.append((path, kwargs))

            def __enter__(self):
                return dict(descriptor)

            def __exit__(self, *_args):
                verified.append("closed")

        class FakeNativeAttachmentDriver:
            def __init__(self, target, dll_path, expected_sha):
                self.target = target
                self.dll_path = dll_path
                self.expected_sha = expected_sha

        native_module = ModuleType("wxbg.native_driver")
        native_module.VerifiedFile = FakeVerifiedFile
        native_module.NativeAttachmentDriver = FakeNativeAttachmentDriver
        win32process = ModuleType("win32process")
        win32process.GetWindowThreadProcessId = lambda _hwnd: (404, 101)
        attachments_module = ModuleType("wxbg.attachments")
        from wxbg.attachments import NATIVE_SHA256
        attachments_module.NATIVE_SHA256 = NATIVE_SHA256
        adapter = SimpleNamespace(pid=101, hwnd=202, created=303.0)
        with patch.dict(sys.modules, {
            "wxbg.native_driver": native_module,
            "wxbg.attachments": attachments_module,
            "win32process": win32process,
        }), patch.object(probe_module, "probe", return_value={"ok": True}) as probe:
            result = probe_module.run(adapter, SESSION_REF, descriptor, deadline=99.0)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(verified[0][0], descriptor["path"])
        self.assertEqual(verified[1], "closed")
        probe.assert_called_once()
        called_adapter, called_driver, called_session, called_file, called_deadline = probe.call_args.args
        self.assertIs(called_adapter, adapter)
        self.assertIsInstance(called_driver, FakeNativeAttachmentDriver)
        self.assertEqual(called_driver.target, {"pid": 101, "hwnd": 202, "created": 303.0, "tid": 404})
        self.assertEqual(called_session, SESSION_REF)
        self.assertEqual(called_file, descriptor)
        self.assertEqual(called_deadline, 99.0)
        self.assertEqual(probe.call_args.kwargs, {"own_only": False})


if __name__ == "__main__":
    unittest.main()
