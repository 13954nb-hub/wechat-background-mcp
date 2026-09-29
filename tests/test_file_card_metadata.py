"""Root regressions for the actual guardian envelope and metadata disclosure."""
import contextlib
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mcp.server.fastmcp.exceptions import ToolError
from wxbg import supervisor
from test_gate import FakeBackend
from test_file_card_integration import (
    _load_gateway, _file_card_result, _background_evidence, _cleanup,
    SESSION_REF, MESSAGE_REF,
)


class FileCardMetadataTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.gateway = _load_gateway(self.directory)

    def call(self, response):
        with patch.object(self.gateway, 'execute', return_value=response):
            return self.gateway.wechat_read_file_card(SESSION_REF, MESSAGE_REF)

    def response(self):
        return {'ok': True, 'worker_started': True, 'result': _file_card_result(),
                'evidence': _background_evidence(), 'cleanup': _cleanup(),
                'recovery': {'status': 'none', 'restored': None}, 'elapsed_seconds': 0.125}

    def test_actual_guardian_success_envelope_is_accepted(self):
        backend = FakeBackend()
        response = supervisor.run_request(
            {'action': 'read_file_card', 'args': {'session_ref': SESSION_REF, 'message_ref': MESSAGE_REF}},
            state_dir=self.directory, backend=backend,
            worker_runner=lambda *_: {'ok': True, 'result': _file_card_result(),
                                      'evidence': _background_evidence()},
            mutex_factory=lambda *_: contextlib.nullcontext())
        self.assertTrue(response['ok'])
        self.assertIn('elapsed_seconds', response)
        result = self.call(response)
        self.assertTrue(result['ok'])
        self.assertEqual(result['result'], _file_card_result())
        self.assertEqual(backend.writes, [1, 0])

    def test_nested_extra_private_fields_never_escape(self):
        response = self.response()
        for key in ('evidence', 'cleanup', 'recovery'):
            response[key]['private_body'] = 'PRIVATE-CARD-FILENAME'
        for phase in ('before', 'after'):
            response['evidence'][phase]['private_body'] = 'PRIVATE-DRAFT'
        result = self.call(response)
        encoded = json.dumps(result)
        self.assertNotIn('PRIVATE', encoded)
        self.assertNotIn('private_body', encoded)

    def test_invalid_metadata_types_are_rejected_without_body(self):
        for key, value in (('elapsed_seconds', True), ('elapsed_seconds', float('nan')),
                           ('elapsed_seconds', -1), ('worker_started', 'private')):
            response = self.response()
            response[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ToolError) as caught:
                self.call(response)
            self.assertNotIn('PRIVATE', str(caught.exception))
        response = self.response()
        response['evidence']['before']['cursor_api'] = 'PRIVATE'
        with self.assertRaises(ToolError) as caught:
            self.call(response)
        self.assertNotIn('PRIVATE', str(caught.exception))
