"""File gateway contracts: real local owned bytes, mocked guardian, no Weixin."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from test_gateway import load_isolated_gateway, worker_success, worker_failure
from wxbg.journal import Journal


class FileGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'owned.txt'
        self.path.write_bytes(b'owned fixture A')
        self.gateway = load_isolated_gateway(self.root / 'state')
        self.summary = dict(ok=True, status='submitted', submitted=True,
                            verification_level='new_local_attachment_card_and_empty_draft',
                            upload_status='interrupted', remote_receipt_verified=False,
                            counts={'submitted': 1}, refs={'conversation': 'self-ref', 'message': 'msg-ref'},
                            background_mode='minimized')
        self.mock = patch.object(self.gateway, 'execute', return_value=worker_success(self.summary))
        self.execute = self.mock.start()
        self.addCleanup(self.mock.stop)

    def send(self, **changes):
        args = dict(session_ref='self-ref', path=str(self.path), operation_id='file-op')
        args.update(changes)
        return self.gateway.wechat_send_file(**args)

    def test_file_bytes_are_bound_to_journal_replay_and_state_is_preserved(self):
        first = self.send()
        repeated = self.send()
        self.assertEqual(first['result'], self.summary)
        self.assertTrue(repeated['replayed'])
        self.assertEqual(repeated['result'], self.summary)
        self.execute.assert_called_once()
        action, args = self.execute.call_args.args
        self.assertEqual(action, 'send_file')
        self.assertEqual(args['file']['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(args['file']['size'], self.path.stat().st_size)
        raw = (self.root / 'state' / 'operations.sqlite3').read_bytes()
        self.assertNotIn(b'owned fixture A', raw)
        self.assertNotIn(str(self.path).encode(), raw)

    def test_changed_bytes_at_same_path_conflict_without_second_dispatch(self):
        self.send()
        self.path.write_bytes(b'owned fixture B')
        with self.assertRaises(ToolError) as caught:
            self.send()
        self.assertEqual(json.loads(str(caught.exception))['code'], 'operation_conflict')
        self.assertEqual(self.execute.call_count, 1)

    def test_expected_hash_mismatch_is_pre_dispatch_rejection(self):
        with self.assertRaises(ToolError) as caught:
            self.send(expected_sha256='0' * 64)
        self.assertIn('file_hash_mismatch', str(caught.exception))
        self.execute.assert_not_called()
        self.assertIsNone(Journal(self.root / 'state' / 'operations.sqlite3').get('file-op'))

    def test_unverified_media_type_is_not_submitted(self):
        self.path = self.path.with_suffix('.png')
        self.path.write_bytes(b'owned bytes')
        with self.assertRaises(ToolError) as caught:
            self.send()
        self.assertIn('unverified_file_type', str(caught.exception))
        self.execute.assert_not_called()

    def test_file_unknown_is_sticky_and_never_reselects(self):
        self.execute.return_value = worker_failure('outcome_unknown', started=True, unknown=True)
        with self.assertRaises(ToolError):
            self.send()
        with self.assertRaises(ToolError):
            self.send()
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(Journal(self.root / 'state' / 'operations.sqlite3').get('file-op')['state'], 'outcome_unknown')

    def test_owned_file_handle_is_held_through_dispatch_and_then_released(self):
        def dispatch(*_):
            with self.assertRaises(PermissionError):
                self.path.write_bytes(b'modification during dispatch')
            return worker_success(self.summary)
        self.execute.side_effect = dispatch
        self.send()
        self.path.write_bytes(b'handle released')
        self.assertEqual(self.path.read_bytes(), b'handle released')


if __name__ == '__main__':
    unittest.main()
