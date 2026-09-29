"""The MCP gateway must recheck untrusted guardian UI read replies."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from test_gateway import load_isolated_gateway
from test_ui_read_guardian_contract import (
    CONTACTS, DESKTOP, MESSAGES, OPEN_EVIDENCE, OPEN_REF, OPEN_RESULT,
    SESSIONS, STATUS,
)


def reply(body, *, evidence=DESKTOP, navigation=None):
    value = {
        'ok': True, 'worker_started': True, 'result': body,
        'evidence': evidence,
        'cleanup': {'restored': True, 'observed': 0, 'errors': []},
    }
    if navigation is not None:
        value['navigation_evidence'] = navigation
    return value


class UIReadGatewayContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = load_isolated_gateway(Path(self.temp.name))

    def test_simple_reads_reject_unverified_success(self):
        for method, args, body in (
            ('wechat_status', (), STATUS),
            ('wechat_list_sessions', ('', 100), SESSIONS),
            ('wechat_read_messages', (50,), MESSAGES),
            ('wechat_list_contacts', ('', 100, 0), CONTACTS),
        ):
            with self.subTest(method=method):
                unverified = reply(body, evidence={'background_observation_passed': False})
                if method == 'wechat_list_contacts':
                    unverified['navigation_evidence'] = {'restored': True}
                with patch.object(self.gateway, 'execute', return_value=unverified):
                    with self.assertRaises(ToolError):
                        getattr(self.gateway, method)(*args)

    def test_open_session_rejects_missing_selection_proof(self):
        with patch.object(self.gateway, 'execute',
                          return_value=reply(OPEN_RESULT)):
            with self.assertRaises(ToolError):
                self.gateway.wechat_open_session(OPEN_REF)

    def test_verified_replies_remain_usable(self):
        for method, args, body, navigation in (
            ('wechat_status', (), STATUS, None),
            ('wechat_list_sessions', ('', 100), SESSIONS, None),
            ('wechat_read_messages', (50,), MESSAGES, None),
            ('wechat_list_contacts', ('', 100, 0), CONTACTS, {'restored': True}),
            ('wechat_open_session', (OPEN_REF,), OPEN_RESULT, OPEN_EVIDENCE),
        ):
            with self.subTest(method=method):
                verified = reply(body, navigation=navigation)
                with patch.object(self.gateway, 'execute', return_value=verified):
                    actual = getattr(self.gateway, method)(*args)
                if method == 'wechat_read_messages':
                    expected = {**verified, 'result': {
                        **verified['result'], 'messages': [{
                            **verified['result']['messages'][0],
                            'sender_role': 'unknown',
                            'sender_role_verified': False,
                            'sender_role_evidence': 'unavailable',
                        }],
                    }}
                else:
                    expected = verified
                self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
