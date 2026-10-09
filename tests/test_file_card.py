import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_ROOT))

from wxbg.policy import AdapterError, stable_ref

try:
    from wxbg import file_card as module
    from wxbg import file_card_contract as contract
except (ImportError, ModuleNotFoundError):
    module = None
    contract = None


CARD_NAME = "檔案\n進度: 42%\n报告.txt\n92B\n正在上傳\n微信电脑版"
CARD_NAME_B = "檔案\n進度: 0%\n报告.txt\n92B\n傳送中斷\n微信电脑版"
FAKE_IDENTITY = "fake-process:1"
SESSION_REF = stable_ref(
    FAKE_IDENTITY, "", "session-runtime", "mmui::ChatSessionCell", "session_item_檔案傳輸"
)


def card_ref(name, runtime="card-runtime"):
    return stable_ref(
        FAKE_IDENTITY, "檔案傳輸", runtime, "mmui::ChatBubbleItemView", "|" + name
    )


MESSAGE_REF = card_ref(CARD_NAME)
CHANGED_REF = card_ref("changed")


class Node:
    def __init__(self, cls, name, runtime, control="ListItem", aid=""):
        self._info = SimpleNamespace(
            class_name=cls, name=name, runtime_id=runtime,
            control_type=control, automation_id=aid,
        )

    @property
    def element_info(self):
        return self._info


class Selection:
    def __init__(self, adapter):
        self.adapter = adapter

    @property
    def CurrentIsSelected(self):
        return self.adapter.selected


class Value:
    def __init__(self, adapter):
        self.adapter = adapter

    @property
    def CurrentValue(self):
        if self.adapter.on_value:
            self.adapter.on_value(self.adapter)
        return self.adapter.text


class FakeAdapter:
    def __init__(self):
        self.identity = "fake-process:1"
        self.title = "檔案傳輸"
        self.text = ""
        self.selected = True
        self.nodes_calls = 0
        self.precondition_calls = 0
        self.order = []
        self.fail_precondition = False
        self.forbidden_calls = []
        self.on_nodes = None
        self.on_card_ref = None
        self.on_value = None
        self.session = Node(
            "mmui::ChatSessionCell", "檔案傳輸\npreview", "session-runtime",
            aid="session_item_檔案傳輸",
        )
        self.session.iface_selection_item = Selection(self)
        self.field = Node(
            "mmui::ChatInputField", self.title, "field-runtime",
            control="Edit", aid="chat_input_field",
        )
        self.field.iface_value = Value(self)
        self.card = Node("mmui::ChatBubbleItemView", CARD_NAME, "card-runtime")
        self.extra_cards = []

    def precondition(self):
        self.precondition_calls += 1
        self.order.append("precondition")
        if self.fail_precondition:
            raise AdapterError("context_conflict")

    def nodes(self):
        self.nodes_calls += 1
        self.order.append("nodes")
        if self.on_nodes:
            self.on_nodes(self, self.nodes_calls)
        self.field.element_info.name = self.title
        return [self.session, self.field, self.card, *self.extra_cards]

    def ref(self, node, context=""):
        if node is self.session:
            return SESSION_REF
        if node is self.card:
            if self.on_card_ref:
                self.on_card_ref(self, node)
            return card_ref(node.element_info.name, str(node.element_info.runtime_id))
        if getattr(self, "duplicate_refs", False) and node.element_info.class_name == "mmui::ChatBubbleItemView":
            return MESSAGE_REF
        return CHANGED_REF

    def open_session(self, *_args, **_kwargs):
        self.forbidden_calls.append("open_session")
        raise AssertionError("file-card reader must not navigate")

    def click(self, *_args, **_kwargs):
        self.forbidden_calls.append("click")
        raise AssertionError("file-card reader must not click")

    def selected_target(self, *_args, **_kwargs):
        self.forbidden_calls.append("selected_target")
        raise AssertionError("file-card reader must not query navigation helper")

    def current_chat(self, *_args, **_kwargs):
        self.forbidden_calls.append("current_chat")
        raise AssertionError("file-card reader must not query navigation helper")

    def draft(self, *_args, **_kwargs):
        self.forbidden_calls.append("draft")
        raise AssertionError("file-card reader must use observed draft only")


class FileCardTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, "file-card reader is not implemented")
        self.assertIsNotNone(contract, "file-card contract is not implemented")
        self.adapter = FakeAdapter()

    def read(self, *, session_ref=SESSION_REF, message_ref=MESSAGE_REF, deadline=None):
        return module.read_file_card(self.adapter, session_ref, message_ref, deadline)

    def test_success_reads_two_fresh_matching_observations_and_returns_metadata_only(self):
        result = self.read()

        self.assertEqual(self.adapter.nodes_calls, 2)
        self.assertEqual(self.adapter.forbidden_calls, [])
        self.assertEqual(set(result), {
            "ok", "status", "verification_level", "background_mode",
            "transfer_indicator", "progress_percent", "upload_status", "display_size",
            "remote_receipt_verified", "stable_message_id", "not_full_history",
            "counts", "refs",
        })
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["verification_level"], "two_matching_local_file_card_observations")
        self.assertEqual(result["transfer_indicator"], "uploading")
        self.assertEqual(result["progress_percent"], 42)
        self.assertEqual(result["upload_status"], "uploading")
        self.assertEqual(result["display_size"], "92B")
        self.assertEqual(result["counts"], {"observations": 2})
        self.assertEqual(result["refs"], {"conversation": SESSION_REF, "message": MESSAGE_REF})
        self.assertTrue(contract.valid_file_card_result(result, SESSION_REF, MESSAGE_REF))
        self.assertNotIn("报告.txt", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("檔案", json.dumps(result, ensure_ascii=False))
        json.dumps(result, ensure_ascii=False)

    def test_refs_use_the_same_stable_ref_grammar_as_the_adapter(self):
        self.assertEqual(self.adapter.ref(self.adapter.session), SESSION_REF)
        self.assertEqual(self.adapter.ref(self.adapter.card, self.adapter.title), MESSAGE_REF)

    def test_three_line_card_reads_two_matching_observations_without_private_metadata(self):
        name = "檔案\nreport.pdf\n70.4K"
        self.adapter.card.element_info.name = name
        message_ref = card_ref(name)
        self.adapter.text = "existing private draft"

        result = self.read(message_ref=message_ref)

        self.assertEqual(self.adapter.nodes_calls, 2)
        self.assertEqual(self.adapter.forbidden_calls, [])
        self.assertEqual(result["counts"], {"observations": 2})
        self.assertEqual(result["refs"], {"conversation": SESSION_REF, "message": message_ref})
        self.assertEqual(result["display_size"], "70.4K")
        self.assertEqual(result["transfer_indicator"], "no_transfer_indicator")
        self.assertEqual(result["upload_status"], "unknown")
        self.assertIsNone(result["progress_percent"])
        self.assertIs(result["remote_receipt_verified"], False)
        self.assertIs(result["stable_message_id"], False)
        self.assertTrue(contract.valid_file_card_result(result, SESSION_REF, message_ref))
        self.assertEqual(self.adapter.text, "existing private draft")
        encoded = json.dumps(result, ensure_ascii=False)
        for private in ("filename", "report.pdf", name, "existing private draft"):
            self.assertNotIn(private, encoded)

    def test_footer_change_does_not_relax_exact_card_identity(self):
        name = "檔案\nreport.pdf\n70.4K"
        self.adapter.card.element_info.name = name

        def add_footer(adapter, call):
            if call == 2:
                adapter.card.element_info.name = name + "\n微信電腦版"

        self.adapter.on_nodes = add_footer
        with self.assertRaises(AdapterError) as caught:
            self.read(message_ref=card_ref(name))

        self.assertEqual(caught.exception.code, "file_card_changed")
        self.assertEqual(self.adapter.nodes_calls, 2)
        self.assertEqual(self.adapter.forbidden_calls, [])

    def test_precondition_immediately_precedes_each_fresh_tree_walk(self):
        self.read()

        node_positions = [index for index, event in enumerate(self.adapter.order) if event == "nodes"]
        self.assertEqual(len(node_positions), 2)
        self.assertTrue(all(index > 0 and self.adapter.order[index - 1] == "precondition"
                            for index in node_positions))

    def test_precondition_failure_happens_before_any_tree_walk(self):
        self.adapter.fail_precondition = True

        with self.assertRaises(AdapterError) as caught:
            self.read()

        self.assertEqual(caught.exception.code, "context_conflict")
        self.assertEqual(self.adapter.nodes_calls, 0)
        self.assertEqual(self.adapter.order, ["precondition"])

    def test_alternating_name_during_ref_calculation_is_rejected(self):
        def reset_cached_name(adapter, call):
            adapter.card.element_info.name = CARD_NAME

        def provider_changes_name_before_ref(adapter, node):
            node.element_info.name = CARD_NAME_B

        self.adapter.on_nodes = reset_cached_name
        self.adapter.on_card_ref = provider_changes_name_before_ref
        with self.assertRaises(AdapterError) as caught:
            self.read(message_ref=card_ref(CARD_NAME_B))

        self.assertEqual(caught.exception.code, "file_card_changed")
        self.assertEqual(self.adapter.nodes_calls, 1)
        self.assertNotIn("CARD_NAME_B", repr(caught.exception))

    def test_nonempty_draft_is_preserved_and_never_published(self):
        self.adapter.text = "existing user draft"

        result = self.read()

        self.assertEqual(self.adapter.text, "existing user draft")
        self.assertNotIn("existing user draft", json.dumps(result))
        self.assertEqual(result["counts"], {"observations": 2})

    def test_invalid_refs_and_deadline_fail_before_any_adapter_work(self):
        cases = [
            ("session_ref", "short", MESSAGE_REF, None, "invalid_session_ref"),
            ("session_ref", "A" * 32, MESSAGE_REF, None, "invalid_session_ref"),
            ("message_ref", SESSION_REF, "short", None, "invalid_message_ref"),
            ("deadline", SESSION_REF, MESSAGE_REF, True, "invalid_deadline"),
            ("deadline", SESSION_REF, MESSAGE_REF, math.nan, "invalid_deadline"),
            ("deadline", SESSION_REF, MESSAGE_REF, math.inf, "invalid_deadline"),
        ]
        for label, session_ref, message_ref, deadline, code in cases:
            with self.subTest(label=label, value=repr(deadline)):
                with self.assertRaises(AdapterError) as caught:
                    self.read(session_ref=session_ref, message_ref=message_ref, deadline=deadline)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.adapter.nodes_calls, 0)
                self.assertEqual(self.adapter.precondition_calls, 0)

    def test_expired_deadline_fails_before_observation(self):
        with patch.object(module.time, "monotonic", return_value=100.0):
            with self.assertRaises(AdapterError) as caught:
                self.read(deadline=100.0)

        self.assertEqual(caught.exception.code, "file_card_budget_exhausted")
        self.assertEqual(self.adapter.nodes_calls, 0)

    def test_card_ref_name_change_is_stale_and_never_falls_back_by_filename(self):
        def change_card(adapter, call):
            if call == 2:
                adapter.card.element_info.name = CARD_NAME.replace("报告.txt", "报告-new.txt")

        self.adapter.on_nodes = change_card
        with self.assertRaises(AdapterError) as caught:
            self.read()

        self.assertEqual(caught.exception.code, "file_card_changed")
        self.assertEqual(self.adapter.nodes_calls, 2)
        self.assertEqual(self.adapter.forbidden_calls, [])

    def test_duplicate_matching_cards_are_rejected(self):
        self.adapter.duplicate_refs = True
        self.adapter.extra_cards.append(Node("mmui::ChatBubbleItemView", CARD_NAME, "card-two"))

        with self.assertRaises(AdapterError) as caught:
            self.read()

        self.assertEqual(caught.exception.code, "ambiguous_file_card")

    def test_draft_change_between_fresh_observations_is_rejected(self):
        def change_draft(adapter, call):
            if call >= 2:
                adapter.text = "changed draft"

        self.adapter.on_nodes = change_draft
        with self.assertRaises(AdapterError) as caught:
            self.read()

        self.assertEqual(caught.exception.code, "draft_conflict")
        self.assertEqual(self.adapter.forbidden_calls, [])

    def test_selection_header_field_and_card_runtime_changes_are_not_accepted(self):
        mutations = (
            ("selection", lambda a: setattr(a, "selected", False), "context_conflict"),
            ("header", lambda a: setattr(a, "title", "other"), "context_conflict"),
            ("field", lambda a: setattr(a.field.element_info, "runtime_id", "new-field"), "context_conflict"),
            ("card_runtime", lambda a: setattr(a.card.element_info, "runtime_id", "new-card"), "file_card_not_found"),
        )
        for label, mutate, code in mutations:
            with self.subTest(label=label):
                self.adapter = FakeAdapter()
                self.adapter.on_nodes = lambda adapter, call, mutate=mutate: mutate(adapter) if call == 2 else None
                with self.assertRaises(AdapterError) as caught:
                    self.read()
                self.assertEqual(caught.exception.code, code)

    def test_parser_layout_failure_is_fixed_and_does_not_publish_raw_error(self):
        self.adapter.card.element_info.name = "檔案\nnot-a-card"

        with self.assertRaises(AdapterError) as caught:
            self.read(message_ref=card_ref("檔案\nnot-a-card"))

        self.assertEqual(caught.exception.code, "file_card_layout_unverified")
        self.assertNotIn("not-a-card", repr(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
