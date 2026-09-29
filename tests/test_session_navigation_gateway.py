"""Public MCP wiring for journaled bounded session navigation."""
import unittest
from unittest.mock import patch

from wxbg import gateway


class SessionNavigationGatewayTests(unittest.TestCase):
    def test_scan_open_session_uses_mutation_journal(self):
        with patch.object(gateway, 'mutate_with_body', return_value={'ok': True}) as mutate:
            gateway.wechat_scan_open_session('Example Group', 4, 'scan-op-1')
        self.assertEqual(mutate.call_args.args[:3], (
            'scan_open_session', {'title': 'Example Group', 'max_steps': 4,
                                  'direction': 'down'}, 'scan-op-1'))
        summary = mutate.call_args.args[3]({
            'ok': True, 'status': 'not_found_in_bounded_scan', 'title': None,
            'verification_level': 'bounded_viewport_observation',
            'counts': {'wheel_steps': 0, 'viewports': 1},
            'background_mode': 'minimized'})
        self.assertNotIn('title', summary)
        self.assertEqual(summary['counts']['viewports'], 1)

    def test_invalid_step_count_rejected_before_dispatch(self):
        with patch.object(gateway, 'mutate_with_body') as mutate:
            with self.assertRaises(Exception):
                gateway.wechat_scan_open_session('Example Group', 5, 'scan-op-2')
        mutate.assert_not_called()

    def test_up_direction_is_journaled_and_invalid_direction_rejected(self):
        with patch.object(gateway, 'mutate_with_body', return_value={'ok': True}) as mutate:
            gateway.wechat_scan_open_session('Example Group', 1, 'scan-op-up', 'up')
            self.assertEqual(mutate.call_args.args[1]['direction'], 'up')
            with self.assertRaises(Exception):
                gateway.wechat_scan_open_session('Example Group', 1, 'bad-direction', 'left')
            self.assertEqual(mutate.call_count, 1)

    def test_catalog_and_capabilities_describe_bounded_bidirectional_scan(self):
        tool = gateway.SERVER._tool_manager.get_tool('wechat_scan_open_session')
        schema = tool.parameters
        self.assertEqual(schema['properties']['direction']['default'], 'down')
        self.assertEqual(schema['properties']['direction']['enum'], ['down', 'up'])
        self.assertEqual(schema['properties']['max_steps']['minimum'], 0)
        self.assertEqual(schema['properties']['max_steps']['maximum'], 4)
        self.assertNotIn('direction', schema['required'])
        capabilities = gateway.wechat_capabilities()
        self.assertIn('bounded_bidirectional_session_scan', capabilities['implemented'])
        self.assertFalse(capabilities['session_scan']['complete_ui_coverage'])
        self.assertFalse(capabilities['session_scan']['end_of_list_proof'])

    def test_scan_gateway_waits_longer_than_scan_worker_cap(self):
        with patch.object(gateway.subprocess, 'Popen') as popen:
            popen.return_value.communicate.return_value = ('{"ok":true}', '')
            response = gateway.execute('scan_open_session',
                                       {'title': 'Example Group', 'max_steps': 2,
                                        'direction': 'up'})
            self.assertTrue(response['ok'])
            self.assertEqual(popen.return_value.communicate.call_args.kwargs['timeout'], 90)


if __name__ == '__main__':
    unittest.main()
