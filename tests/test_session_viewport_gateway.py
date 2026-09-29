"""Public MCP gateway wiring for bounded Chats viewport enumeration."""
import asyncio
import unittest
from unittest.mock import patch

from wxbg import gateway
from wxbg.journal import _summary_json


class ViewportGatewayTests(unittest.TestCase):
    def test_tool_journals_metadata_but_not_observed_titles(self):
        with patch.object(gateway, 'mutate_with_body', return_value={'ok': True}) as mutate:
            gateway.wechat_scan_session_viewports(2, 'viewport-op-1', 'up')
        self.assertEqual(mutate.call_args.args[:3], (
            'scan_session_viewports', {'max_steps': 2, 'direction': 'up'},
            'viewport-op-1'))
        summary = mutate.call_args.args[3]({
            'ok': True, 'status': 'bounded_complete',
            'counts': {'wheel_steps': 2, 'viewports': 3},
            'viewports': [{'index': 0, 'rows': [{'title': '私密群',
                                                'fully_visible': True}]}],
            'coverage': 'bounded_ui_viewports', 'end_verified': False,
            'background_mode': 'minimized',
        })
        self.assertNotIn('viewports', summary)
        self.assertNotIn('私密群', str(summary))
        self.assertIn('bounded_complete', _summary_json(summary))

    def test_invalid_requests_do_not_start_journal(self):
        with patch.object(gateway, 'mutate_with_body') as mutate:
            for steps, operation_id, direction in (
                (5, 'valid-op', 'down'),
                (True, 'valid-op', 'down'),
                (0, '', 'down'),
                (0, 'valid-op', 'sideways'),
            ):
                with self.subTest(steps=steps, operation_id=operation_id,
                                  direction=direction):
                    with self.assertRaises(Exception):
                        gateway.wechat_scan_session_viewports(
                            steps, operation_id, direction)
        mutate.assert_not_called()

    def test_catalog_and_capabilities_state_bounded_coverage(self):
        tool = gateway.SERVER._tool_manager.get_tool(
            'wechat_scan_session_viewports')
        schema = tool.parameters
        self.assertEqual(schema['properties']['max_steps']['maximum'], 4)
        self.assertEqual(schema['properties']['direction']['enum'], ['down', 'up'])
        names = {tool.name for tool in asyncio.run(gateway.SERVER.list_tools())}
        self.assertIn('wechat_scan_session_viewports', names)
        capabilities = gateway.wechat_capabilities()
        self.assertIn('bounded_session_viewport_enumeration',
                      capabilities['implemented'])
        self.assertFalse(capabilities['session_scan']['complete_ui_coverage'])

    def test_gateway_uses_existing_extended_scan_timeout(self):
        with patch.object(gateway.subprocess, 'Popen') as popen:
            popen.return_value.communicate.return_value = ('{"ok":true}', '')
            self.assertTrue(gateway.execute('scan_session_viewports', {})['ok'])
            self.assertEqual(
                popen.return_value.communicate.call_args.kwargs['timeout'], 90)


if __name__ == '__main__':
    unittest.main()
