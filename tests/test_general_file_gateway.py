"""Gateway-only PDF/ZIP submission coverage with local owned fixtures."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

from test_gateway import load_isolated_gateway, worker_failure, worker_success
from wxbg.journal import Journal


MAX_FILE_BYTES = 1024 * 1024


class GeneralFileGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="general-file-gateway-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.gateway = load_isolated_gateway(self.state)
        self.summary = {
            "ok": True,
            "status": "submitted",
            "submitted": True,
            "verification_level": "new_local_attachment_card_and_empty_draft",
            "upload_status": "interrupted",
            "remote_receipt_verified": False,
            "counts": {"submitted": 1},
            "refs": {"conversation": "self-ref", "message": "msg-ref"},
            "background_mode": "minimized",
        }
        self.execute = self.enterContext(
            patch.object(self.gateway, "execute", return_value=worker_success(self.summary))
        )

    def fixture(self, suffix, payload=None):
        path = self.root / f"owned{suffix}"
        if payload is None:
            payload = {
                ".pdf": b"%PDF-test-owned-A\n",
                ".zip": b"PK\x03\x04test-owned-A\n",
            }[suffix]
        path.write_bytes(payload)
        return path

    def send(self, path, operation_id):
        return self.gateway.wechat_send_file(
            session_ref="self-ref", path=str(path), operation_id=operation_id
        )

    def test_capabilities_and_description_advertise_only_verified_general_suffixes(self):
        capabilities = self.gateway.wechat_capabilities()
        self.assertEqual(
            capabilities["file_submission"],
            {
                "extensions": [".txt", ".pdf", ".zip"],
                "max_bytes": MAX_FILE_BYTES,
                "local_submission_only": True,
                "remote_receipt_verified": False,
            },
        )
        description = self.gateway.wechat_send_file.__doc__ or ""
        for suffix in (".txt", ".pdf", ".zip"):
            self.assertIn(suffix, description)
        self.assertIn("1 MiB", description)

    def test_pdf_and_zip_each_dispatch_once_with_fresh_hash_bound_descriptor(self):
        for suffix in (".pdf", ".zip"):
            with self.subTest(suffix=suffix):
                path = self.fixture(suffix)
                result = self.send(path, f"fresh-{suffix[1:]}")
                self.assertEqual(result["result"], self.summary)

        self.assertEqual(self.execute.call_count, 2)
        calls = [call.args for call in self.execute.call_args_list]
        self.assertEqual([action for action, _args in calls], ["send_file", "send_file"])
        for (_action, args), suffix in zip(calls, (".pdf", ".zip")):
            path = self.root / f"owned{suffix}"
            self.assertEqual(args["file"]["name"], path.name)
            self.assertEqual(args["file"]["size"], path.stat().st_size)
            self.assertEqual(
                args["file"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )

    def test_same_operation_id_replays_without_second_dispatch(self):
        path = self.fixture(".pdf")
        first = self.send(path, "replay-pdf")
        replay = self.send(path, "replay-pdf")
        self.assertEqual(first["result"], self.summary)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], self.summary)
        self.execute.assert_called_once()

    def test_changed_bytes_at_same_path_conflict_without_second_dispatch(self):
        path = self.fixture(".zip")
        self.send(path, "conflict-zip")
        path.write_bytes(b"PK\x03\x04test-owned-B\n")
        with self.assertRaises(ToolError) as caught:
            self.send(path, "conflict-zip")
        self.assertEqual(json.loads(str(caught.exception))["code"], "operation_conflict")
        self.execute.assert_called_once()

    def test_oversized_pdf_is_rejected_before_dispatch(self):
        path = self.fixture(".pdf", b"P" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ToolError) as caught:
            self.send(path, "oversized-pdf")
        self.assertEqual(json.loads(str(caught.exception))["code"], "file_size_mismatch")
        self.execute.assert_not_called()
        self.assertIsNone(Journal(self.state / "operations.sqlite3").get("oversized-pdf"))

    def test_unknown_outcome_is_sticky_and_never_dispatches_again(self):
        path = self.fixture(".zip")
        self.execute.return_value = worker_failure("outcome_unknown", started=True, unknown=True)
        with self.assertRaises(ToolError):
            self.send(path, "unknown-zip")
        with self.assertRaises(ToolError) as caught:
            self.send(path, "unknown-zip")
        self.assertIn("outcome_unknown", str(caught.exception))
        self.execute.assert_called_once()
        self.assertEqual(
            Journal(self.state / "operations.sqlite3").get("unknown-zip")["state"],
            "outcome_unknown",
        )


if __name__ == "__main__":
    unittest.main()
