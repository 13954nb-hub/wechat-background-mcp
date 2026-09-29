"""Behavioral checks for the text-send-only worker and caller deadlines."""
import io
import json
import types
import unittest
from unittest.mock import patch

from wxbg import gateway, supervisor


class SendTimeoutBudgetTests(unittest.TestCase):
    def test_gateway_wait_is_extended_only_for_text_send_actions(self):
        observed = []

        class Child:
            def communicate(self, payload, timeout):
                observed.append((json.loads(payload)['action'], timeout))
                return json.dumps({'ok': True}), ''

        with patch.object(gateway.subprocess, 'Popen', return_value=Child()):
            gateway.execute('send_text', {})
            gateway.execute('send_at_username', {})
            gateway.execute('list_sessions', {})

        self.assertEqual(observed, [
            ('send_text', 50), ('send_at_username', 50), ('list_sessions', 45),
        ])

    def test_supervisor_cli_extends_only_text_send_worker_budget(self):
        observed = []

        def fake_run(request, timeout=30):
            observed.append((request['action'], timeout))
            return {'ok': True}

        with patch.object(supervisor, 'run_request', side_effect=fake_run):
            for action in ('send_text', 'send_at_username', 'list_sessions'):
                request = {'action': action, 'args': {}}
                incoming = types.SimpleNamespace(
                    buffer=io.BytesIO(json.dumps(request).encode('utf-8')),
                )
                with patch.object(supervisor.sys, 'stdin', incoming), \
                        patch.object(supervisor.sys, 'stdout', io.StringIO()):
                    supervisor.main()

        self.assertEqual(observed, [
            ('send_text', 40), ('send_at_username', 40), ('list_sessions', 30),
        ])


if __name__ == '__main__':
    unittest.main()
