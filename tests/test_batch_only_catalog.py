"""Release-boundary checks for the isolated batch-only candidate."""
import asyncio
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbg import __version__, gateway, readstore_provider, supervisor, worker


FORMAL_TOOLS = {
    "wechat_capabilities", "wechat_status", "wechat_list_sessions", "wechat_read_inbox",
    "wechat_probe_session_container", "wechat_scan_open_session",
    "wechat_scan_session_viewports",
    "wechat_list_conversations",
    "wechat_search", "wechat_read_new_messages", "wechat_open_session", "wechat_list_contacts",
    "wechat_wait_for_ui_hint", "wechat_read_messages", "wechat_read_file_card",
    "wechat_scroll_messages", "wechat_read_history_span", "wechat_read_contact_span",
    "wechat_get_draft", "wechat_set_draft", "wechat_send_text", "wechat_send_at_username", "wechat_send_file",
    "wechat_operation_status", "wechat_send_image", "wechat_get_attachment",
}


class BatchOnlyCatalogTests(unittest.TestCase):
    def test_catalog_includes_batch_and_session_navigation_tools(self):
        tools = asyncio.run(gateway.SERVER.list_tools())
        names = {tool.name for tool in tools}

        self.assertEqual(names, FORMAL_TOOLS | {"wechat_batch_read_messages", "wechat_watch_new_messages", "wechat_view_attachment_handoff"})
        self.assertEqual(len(names), 29)

    def test_batch_modules_are_part_of_the_staged_package(self):
        for name in (
            "wxbg.batch_read_contract",
            "wxbg.batch_read_provider",
            "wxbg.readstore_batch_public",
            "wxbg.readstore_batch_provider",
        ):
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.util.find_spec(name))

    def test_batch_action_is_routed_by_readstore_provider(self):
        marker = {"ok": True, "source": "batch-test"}
        invoked = []

        def run_batch_read(args, **kwargs):
            invoked.append((args, kwargs))
            return marker

        fake = SimpleNamespace(run_batch_read=run_batch_read)
        with patch.dict(sys.modules, {"wxbg.readstore_batch_provider": fake}):
            try:
                result = readstore_provider.run(
                    "readstore_batch_read_new", {"sentinel": 1},
                    target={"pid": 1}, deadline=123.0,
                )
            except readstore_provider.ProviderError as error:
                self.fail(f"batch action was not routed: {error}")

        self.assertIs(result, marker)
        self.assertEqual(invoked, [({"sentinel": 1}, {
            "target": {"pid": 1}, "deadline": 123.0,
            "open_stores": readstore_provider.open_stores,
        })])

    def test_worker_and_supervisor_allow_only_the_batch_read_extension(self):
        action = "readstore_batch_read_new"
        self.assertIn(action, worker.READSTORE_ACTIONS)
        self.assertIn(action, supervisor.READSTORE_ACTIONS)
        candidate_only_actions = {
            "download_attachment", "quote_reply", "mention_member",
            "readstore_download_attachment", "native_quote_reply", "native_mention_member",
        }
        self.assertFalse(candidate_only_actions & worker.READSTORE_ACTIONS)
        self.assertFalse(candidate_only_actions & supervisor.READSTORE_ACTIONS)

    def test_download_quote_mention_and_upload_reconciliation_stay_absent(self):
        for name in (
            "wechat_download_attachment",
            "wechat_quote_reply",
            "wechat_mention_member",
        ):
            self.assertFalse(hasattr(gateway, name), name)
        status_tool = gateway.SERVER._tool_manager.get_tool("wechat_operation_status")
        self.assertEqual(set(status_tool.fn_metadata.arg_model.model_fields), {"operation_id"})
        capabilities = gateway.wechat_capabilities()
        self.assertNotIn("attachment_download", capabilities)
        self.assertNotIn("upload_reconciliation", capabilities)

    def test_release_version_and_capability_are_consistent(self):
        root = Path(__file__).parents[1]
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        capabilities = gateway.wechat_capabilities()

        self.assertEqual(metadata["project"]["version"], "2.6.0")
        self.assertEqual(__version__, metadata["project"]["version"])
        self.assertEqual(capabilities["version"], metadata["project"]["version"])
        self.assertEqual(capabilities.get("batch_messages", {}).get("status"), "released")
        self.assertTrue(capabilities.get("incremental_messages", {}).get("empty_message_table_bootstrap"))


if __name__ == "__main__":
    unittest.main()
