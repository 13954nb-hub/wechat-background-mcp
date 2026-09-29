"""Pure gateway/worker contracts for contact navigation; no live COM or UI."""
import asyncio
from contextlib import redirect_stdout, nullcontext
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from wxbg.policy import AdapterError
from wxbg import worker
from test_gateway import load_isolated_gateway
from test_adapter import load_isolated_adapter
from test_ui_read_guardian_contract import CONTACTS, DESKTOP
from worker_dpi_stub import worker_monitor_module


class ContactGatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.gateway = load_isolated_gateway(self.state)

    def test_contact_read_forwards_filter_and_does_not_persist_contact_payload(self):
        body = {**CONTACTS, 'query': 'synthetic', 'limit': 3,
                'contacts': [{'contact_ref': 'c' * 32,
                              'display_text': 'synthetic private contact'}]}
        expected = {'ok': True, 'worker_started': True, 'result': body,
                    'navigation_evidence': {'restored': True}, 'evidence': DESKTOP,
                    'cleanup': {'restored': True, 'observed': 0, 'errors': []}}
        with patch.object(self.gateway, 'execute', return_value=expected) as execute:
            self.assertEqual(self.gateway.wechat_list_contacts('synthetic', 3), expected)
        execute.assert_called_once_with('list_contacts', {'query': 'synthetic', 'limit': 3, 'scroll_steps': 0})
        self.assertFalse((self.state / 'operations.sqlite3').exists())

    def test_failed_restore_is_reported_as_error_with_navigation_evidence(self):
        response = {'ok': False, 'error': {'code': 'contacts_context_restore_failed'},
                    'navigation_evidence': {'attempted': True, 'restored': False}}
        with patch.object(self.gateway, 'execute', return_value=response):
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_list_contacts()
        self.assertEqual(json.loads(str(caught.exception)), response)

    def test_mcp_rejects_bool_float_and_string_limits_before_dispatch(self):
        for invalid in (True, 1.0, '1'):
            with self.subTest(value=invalid), patch.object(self.gateway, 'execute', return_value={'ok': True, 'result': {}}) as execute:
                with self.assertRaises(ToolError):
                    asyncio.run(self.gateway.SERVER.call_tool('wechat_list_contacts', {'limit': invalid}))
                execute.assert_not_called()

    def test_tool_describes_navigation_side_effect_and_separate_full_directory_limit(self):
        tool = next(t for t in asyncio.run(self.gateway.SERVER.list_tools()) if t.name == 'wechat_list_contacts')
        self.assertFalse(tool.annotations.readOnlyHint)
        self.assertFalse(tool.annotations.destructiveHint)
        self.assertTrue(tool.annotations.idempotentHint)
        self.assertFalse(tool.annotations.openWorldHint)
        capability = self.gateway.wechat_capabilities()
        self.assertIn('exposed_contacts', capability['implemented'])
        self.assertIn('full_contact_directory', capability['not_implemented'])

    def test_scroll_steps_are_forwarded_without_a_mutation_journal(self):
        body = {**CONTACTS, 'query': 'q', 'limit': 2,
                'contacts': [{'contact_ref': 'c' * 32, 'display_text': 'q contact'}],
                'scroll_steps': 12, 'query_scope': 'requested_contact_view_only',
                'scroll_origin': 'observed_top', 'viewport_changed': True}
        verified = {'ok': True, 'worker_started': True, 'result': body,
                    'navigation_evidence': {'restored': True, 'scroll': {'restored': True}},
                    'evidence': DESKTOP,
                    'cleanup': {'restored': True, 'observed': 0, 'errors': []}}
        with patch.object(self.gateway, 'execute', return_value=verified) as execute:
            self.gateway.wechat_list_contacts('q', 2, scroll_steps=12)
        execute.assert_called_once_with('list_contacts', {'query': 'q', 'limit': 2, 'scroll_steps': 12})
        self.assertFalse((self.state / 'operations.sqlite3').exists())

    def test_mcp_scroll_steps_reject_coercion_before_dispatch(self):
        for invalid in (True, 1.0, '1'):
            with self.subTest(value=invalid), patch.object(self.gateway, 'execute') as execute:
                with self.assertRaises(ToolError):
                    asyncio.run(self.gateway.SERVER.call_tool('wechat_list_contacts', {'scroll_steps': invalid}))
                execute.assert_not_called()

    def test_guardian_requires_scroll_evidence_independently_of_chat_and_gate(self):
        from wxbg import supervisor
        from test_gate import FakeBackend
        for scroll in (None, {'restored': False}, {'restored': True}):
            for result_restored in (False, True):
                backend = FakeBackend(); backend.state_dir = self.state
                body = {**CONTACTS, 'limit': 100, 'scroll_steps': 1,
                        'query_scope': 'requested_contact_view_only',
                        'scroll_origin': 'observed_top', 'viewport_changed': True,
                        'original_contacts_view_restored': result_restored}
                reply = {'ok': True, 'navigation_evidence': {'restored': True, 'scroll': scroll},
                         'result': body, 'evidence': DESKTOP}
                result = supervisor.run_request({'action': 'list_contacts', 'args': {'scroll_steps': 1}},
                    state_dir=self.state, backend=backend, worker_runner=lambda *_: reply,
                    mutex_factory=lambda *_: nullcontext())
                self.assertEqual(result['ok'], bool(scroll and scroll['restored'] and result_restored))
                self.assertTrue(result['cleanup']['restored'])

    def test_adapter_dispatches_only_to_contact_read_orchestration(self):
        module = load_isolated_adapter()
        adapter = object.__new__(module.Adapter)
        expected = {'contacts': [], 'not_full_directory': True}
        contact_module = types.ModuleType('wxbg.contact_actions')
        contact_module.list_contacts = lambda candidate, **args: (candidate, args, expected)
        with patch.dict(sys.modules, {'wxbg.contact_actions': contact_module}):
            self.assertEqual(adapter.dispatch('list_contacts', {'query': 'q', 'limit': 5}),
                             (adapter, {'query': 'q', 'limit': 5}, expected))

    def test_real_contact_orchestration_result_is_accepted_by_guardian_contract(self):
        from wxbg import contact_actions, supervisor
        from test_contact_actions import FakeAdapter
        from test_gate import FakeBackend
        adapter = FakeAdapter()
        backend = FakeBackend()
        backend.state_dir = self.state
        def run_worker(request, timeout):
            result = contact_actions.list_contacts(adapter, **request['args'])
            return {'ok': True, 'result': result,
                    'navigation_evidence': adapter.navigation_evidence,
                    'evidence': DESKTOP}
        result = supervisor.run_request({'action': 'list_contacts', 'args': {'limit': 1}},
            state_dir=self.state, backend=backend, worker_runner=run_worker,
            mutex_factory=lambda *_: nullcontext())
        self.assertTrue(result['ok'], result.get('error'))
        self.assertTrue(result['result']['original_conversation_restored'])
        self.assertTrue(result['navigation_evidence']['restored'])
        self.assertTrue(result['cleanup']['restored'])
        self.assertEqual(result['result']['count'], 1)


class ContactWorkerTests(unittest.TestCase):
    def invoke(self, *, read_failure=False, background_passed=True, monitor_raises=False):
        evidence = {'attempted': True, 'restored': not read_failure, 'primary_error': 'owned_failure' if read_failure else None}
        class FakeAdapter:
            submission_started = False
            navigation_evidence = evidence
            def __init__(self, target):
                pass
            def dispatch(self, action, args):
                if read_failure:
                    raise AdapterError('contacts_context_restore_failed')
                return {'contacts': [], 'original_conversation_restored': True}
        class FakeMonitor:
            def __init__(self, pid, hwnd):
                pass
            def start(self):
                return self
            def stop(self):
                if monitor_raises:
                    raise RuntimeError('owned monitor error')
                return {'background_observation_passed': background_passed}
        adapter_module = types.ModuleType('wxbg.adapter')
        adapter_module.Adapter = FakeAdapter
        monitor_module = worker_monitor_module(FakeMonitor)
        out = io.StringIO()
        request = {'action': 'list_contacts', 'args': {}, 'target': {'pid': 123, 'hwnd': 456}}
        with patch.dict(sys.modules, {'wxbg.adapter': adapter_module, 'wxbg.monitor': monitor_module}), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), redirect_stdout(out):
            worker.main()
        response = json.loads(out.getvalue())
        self.assertEqual(response['navigation_evidence'], evidence)
        return response

    def test_navigation_evidence_survives_success(self):
        self.assertTrue(self.invoke()['ok'])

    def test_navigation_evidence_survives_restore_failure(self):
        response = self.invoke(read_failure=True)
        self.assertFalse(response['ok'])
        self.assertFalse(response['error']['submission_started'])

    def test_navigation_evidence_survives_background_failure(self):
        self.assertEqual(self.invoke(background_passed=False)['error']['code'], 'background_side_effect')

    def test_navigation_evidence_survives_monitor_exception(self):
        self.assertEqual(self.invoke(monitor_raises=True)['error']['code'], 'monitor_failed')


if __name__ == '__main__':
    unittest.main()
