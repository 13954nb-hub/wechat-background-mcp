"""Gateway-level route checks for bounded read tools and Computer Use handoffs.

All worker calls are mocked. These tests never connect to Weixin or read real chats.
"""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image
from mcp.types import ImageContent, ResourceLink

from test_gateway import load_isolated_gateway
from wxbg.attachment_resources import AttachmentResourceStore


ROOM = "synthetic-attachment@chatroom"
EPOCH = "a" * 64
IDENTITY = {
    "chat_md5": hashlib.md5(ROOM.encode("utf-8")).hexdigest(),
    "shard_id": "message_0",
    "local_id": 1,
    "server_id": 2,
    "rowid": 3,
}


def _file_metadata(data):
    return {
        "ok": True,
        "result": {
            "filename": "owned.txt",
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "account_epoch": EPOCH,
            "message_identity": dict(IDENTITY),
            "media_kind": "file",
            "representation": "original",
            "download_triggered": False,
            "snapshot_only": True,
            "mime_type": "application/octet-stream",
        },
    }


def _png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), (20, 40, 60)).save(stream, format="PNG")
    return stream.getvalue()


class GatewayReadonlyRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = load_isolated_gateway(Path(self.temp.name))

    def test_new_message_tool_routes_exact_request_to_public_validator(self):
        expected = {"ok": True, "result": {"items": [], "next_cursor": {"v": 1}}}
        with patch.object(self.gateway, "execute", return_value={"opaque": "response"}) as execute, \
             patch("wxbg.readstore_new_public.new_result_or_error", return_value=expected) as validate:
            result = self.gateway.wechat_read_new_messages(ROOM, 7, None, "now")

        self.assertEqual(result, expected)
        execute.assert_called_once_with("readstore_read_new", {
            "conversation_key": ROOM, "limit": 7, "cursor": None, "start_from": "now"})
        validate.assert_called_once_with(
            {"opaque": "response"}, conversation_key=ROOM, limit=7)

    def test_search_routes_exact_message_scope_and_rejects_invalid_scope(self):
        expected = {"ok": True, "result": {"items": [], "count": 0, "has_more": False}}
        with patch.object(self.gateway, "execute", return_value={"opaque": "search"}) as execute, \
             patch("wxbg.readstore_search_public.search_result_or_error",
                   return_value=expected) as validate:
            result = self.gateway.wechat_search("needle", "messages", ROOM, 5, None)

        self.assertEqual(result, expected)
        execute.assert_called_once_with("readstore_search", {
            "scope": "messages", "keyword": "needle", "limit": 5,
            "cursor": None, "conversation_key": ROOM})
        validate.assert_called_once_with({"opaque": "search"}, scope="messages",
            keyword="needle", limit=5, conversation_key=ROOM)

        with patch.object(self.gateway, "execute") as execute:
            with self.assertRaises(ToolError):
                self.gateway.wechat_search("needle", "unsupported")
        execute.assert_not_called()

    def test_watch_routes_bounded_poll_parameters_and_batch_reader(self):
        conversations = [{"conversation_key": ROOM, "cursor": {"next": 3}}]
        expected = {"ok": True, "status": "timeout", "items": []}
        with patch("wxbg.watch_batch.poll_batch_messages", return_value=expected) as poll:
            result = self.gateway.wechat_watch_new_messages(
                EPOCH, conversations, 2, max_total=9, poll_interval_ms=1000)

        self.assertEqual(result, expected)
        kwargs = poll.call_args.kwargs
        self.assertIs(kwargs["read_batch"], self.gateway.wechat_batch_read_messages)
        self.assertEqual(kwargs["account_epoch"], EPOCH)
        self.assertEqual(kwargs["conversations"], conversations)
        self.assertEqual(kwargs["wait_seconds"], 2)
        self.assertEqual(kwargs["max_total"], 9)
        self.assertEqual(kwargs["poll_interval_ms"], 1000)

    def test_probe_session_container_validates_title_and_routes_read_only(self):
        expected = {"ok": True, "result": {"status": "unsupported"}}
        with patch.object(self.gateway, "execute", return_value=expected) as execute:
            self.assertEqual(
                self.gateway.wechat_probe_session_container("Exact synthetic title"),
                expected)
        execute.assert_called_once_with(
            "probe_session_container", {"title": "Exact synthetic title"})

        with patch.object(self.gateway, "execute") as execute:
            with self.assertRaises(ToolError):
                self.gateway.wechat_probe_session_container("  ")
        execute.assert_not_called()

    def test_get_draft_routes_read_only_and_preserves_worker_error(self):
        expected = {"ok": True, "result": {"draft": "synthetic owned draft"}}
        with patch.object(self.gateway, "execute", return_value=expected) as execute:
            self.assertEqual(self.gateway.wechat_get_draft(), expected)
        execute.assert_called_once_with("get_draft", {})

        failure = {"ok": False, "error": {"code": "unverified_geometry"}}
        with patch.object(self.gateway, "execute", return_value=failure) as execute:
            with self.assertRaises(ToolError):
                self.gateway.wechat_get_draft()
        execute.assert_called_once_with("get_draft", {})

    def test_get_attachment_rejects_wrong_chat_identity_before_worker_dispatch(self):
        wrong_identity = {**IDENTITY, "chat_md5": "0" * 32}
        with patch.object(self.gateway, "execute") as execute:
            with self.assertRaises(ToolError):
                self.gateway.wechat_get_attachment(ROOM, EPOCH, wrong_identity)
        execute.assert_not_called()

    def test_get_attachment_file_returns_exact_memory_resource(self):
        data = b"owned file bytes"
        metadata = _file_metadata(data)
        store = AttachmentResourceStore(clock=lambda: 1.0)
        with patch.object(self.gateway, "ATTACHMENT_RESOURCES", store), \
             patch.object(self.gateway, "execute", return_value={"worker": "reply"}) as execute, \
             patch("wxbg.readstore_attachment_public.attachment_result_or_error",
                   return_value=(metadata, data)) as validate:
            response = self.gateway.wechat_get_attachment(ROOM, EPOCH, dict(IDENTITY))

        execute.assert_called_once_with("readstore_get_attachment", {
            "conversation_key": ROOM, "account_epoch": EPOCH,
            "message_identity": IDENTITY})
        validate.assert_called_once_with({"worker": "reply"}, conversation_key=ROOM,
            account_epoch=EPOCH, message_identity=IDENTITY)
        self.assertEqual(len(response.content), 2)
        self.assertEqual(response.content[0].type, "text")
        self.assertIsInstance(response.content[1], ResourceLink)
        link = response.content[1]
        self.assertEqual(store.read(str(link.uri)), data)
        self.assertFalse(response.structuredContent["result"]["inline_image_available"])

    def test_get_attachment_static_image_returns_inline_image_and_exact_resource(self):
        data = _png_bytes()
        metadata = {
            "ok": True,
            "result": {"filename": "owned.png", "sha256": hashlib.sha256(data).hexdigest(),
                       "mime_type": "image/png", "media_kind": "image"},
        }
        store = AttachmentResourceStore(clock=lambda: 1.0)
        with patch.object(self.gateway, "ATTACHMENT_RESOURCES", store), \
             patch.object(self.gateway, "execute", return_value={"worker": "reply"}), \
             patch("wxbg.readstore_attachment_public.attachment_result_or_error",
                   return_value=(metadata, data)):
            response = self.gateway.wechat_get_attachment(ROOM, EPOCH, dict(IDENTITY))

        image_blocks = [part for part in response.content if isinstance(part, ImageContent)]
        links = [part for part in response.content if isinstance(part, ResourceLink)]
        self.assertEqual(len(image_blocks), 1)
        self.assertEqual(image_blocks[0].mimeType, "image/png")
        self.assertEqual(len(links), 1)
        self.assertEqual(store.read(str(links[0].uri)), data)
        self.assertTrue(response.structuredContent["result"]["inline_image_available"])

    def test_attachment_view_handoff_never_claims_view_or_dispatch(self):
        with patch.object(self.gateway, "execute") as execute:
            result = self.gateway.wechat_view_attachment_handoff(
                "image", conversation_title_hint="unverified title")

        self.assertFalse(result["viewed"])
        self.assertFalse(result["dispatch_performed"])
        self.assertFalse(result["bytes_verified"])
        self.assertFalse(result["result"]["server_can_invoke_computer_use"])
        self.assertEqual(result["result"]["unverified_target_hints"]["conversation_title"],
                         "unverified title")
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
