"""Protocol/guardian/passive-worker checks using owned fakes, never Weixin."""
import asyncio
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
from wxbg import supervisor, worker
from wxbg.policy import AdapterError
from test_event_contract import event_evidence, hint_result
from test_gateway import load_isolated_gateway
from test_gate import FakeBackend


class HintGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name); self.gateway = load_isolated_gateway(self.state)
        self.assertTrue(hasattr(self.gateway, 'wechat_wait_for_ui_hint'), 'finite UI hint tool is missing')

    def test_read_wait_forwards_duration_without_mutation_journal(self):
        expected = {'ok': True, 'result': hint_result(duration=3), 'event_evidence': event_evidence()}
        with patch.object(self.gateway, 'execute', return_value=expected) as dispatch:
            self.assertEqual(self.gateway.wechat_wait_for_ui_hint(3), expected)
        dispatch.assert_called_once_with('wait_for_ui_hint', {'timeout_seconds': 3})
        self.assertFalse((self.state/'operations.sqlite3').exists())

    def test_schema_and_mcp_reject_bad_duration_before_dispatch(self):
        tools = asyncio.run(self.gateway.SERVER.list_tools())
        tool = next(t for t in tools if t.name == 'wechat_wait_for_ui_hint')
        prop = tool.inputSchema['properties']['timeout_seconds']
        self.assertEqual((prop['type'], prop['minimum'], prop['maximum']), ('integer', 1, 60))
        self.assertTrue(tool.annotations.readOnlyHint)
        for invalid in (True, 1.0, '1', 0, 61):
            with patch.object(self.gateway, 'execute') as dispatch, self.assertRaises(ToolError):
                asyncio.run(self.gateway.SERVER.call_tool('wechat_wait_for_ui_hint', {'timeout_seconds': invalid}))
            dispatch.assert_not_called()

    def test_direct_call_also_rejects_coercion_and_out_of_range(self):
        for invalid in (True, 1.0, '1', 0, 61):
            with patch.object(self.gateway, 'execute') as dispatch, self.assertRaises(ToolError):
                self.gateway.wechat_wait_for_ui_hint(invalid)
            dispatch.assert_not_called()

    def test_unknown_filters_are_rejected_before_dispatch(self):
        tool = next(t for t in asyncio.run(self.gateway.SERVER.list_tools())
                    if t.name == 'wechat_wait_for_ui_hint')
        self.assertIs(tool.inputSchema.get('additionalProperties'), False)
        for key, value in (('session_ref', 'synthetic'), ('filter', {}), ('continuous', True)):
            with patch.object(self.gateway, 'execute') as dispatch, self.assertRaises(ToolError):
                asyncio.run(self.gateway.SERVER.call_tool(
                    'wechat_wait_for_ui_hint', {'timeout_seconds': 1, key: value}))
            dispatch.assert_not_called()

    def test_cleanup_failure_is_tool_error_and_preserves_evidence(self):
        expected = {'ok': False, 'error': {'code': 'EVENT_CLEANUP_UNVERIFIED'}, 'event_evidence': event_evidence()}
        expected['event_evidence']['owner_exited'] = None
        with patch.object(self.gateway, 'execute', return_value=expected), self.assertRaises(ToolError) as caught:
            self.gateway.wechat_wait_for_ui_hint(1)
        self.assertEqual(json.loads(str(caught.exception)), expected)

    def test_capabilities_do_not_call_ui_hint_a_full_message_listener(self):
        value = self.gateway.wechat_capabilities()
        self.assertIn('bounded_ui_hint', value['implemented'])
        self.assertIn('continuous_message_listener', value['not_implemented'])


class HintGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend(); self.backend.state_dir = self.directory

    def run_guardian(self, runner, **extra):
        return supervisor.run_request({'action':'wait_for_ui_hint','args':{'timeout_seconds':1},**extra},
            state_dir=self.directory, backend=self.backend, worker_runner=runner,
            mutex_factory=lambda *_: nullcontext())

    def good(self):
        return {'ok':True,'result':hint_result(),'event_evidence':event_evidence(),
                'evidence':{'background_observation_passed':True}}

    def test_guardian_supplies_its_own_absolute_deadline(self):
        captured = {}
        def run(request, timeout):
            captured.update(request)
            self.assertAlmostEqual(request['deadline'], 100.0 + supervisor.MAX_HINT_WORKER_SECONDS)
            return self.good()
        with patch.object(supervisor.time, 'monotonic', return_value=100.0):
            result = self.run_guardian(run, deadline=9999999999)
        self.assertTrue(result['ok'], result)

    def test_cleanup_evidence_is_forwarded_and_checked_independently_of_gate(self):
        result = self.run_guardian(lambda *_: self.good())
        self.assertEqual(result.get('event_evidence'), event_evidence())
        self.assertTrue(result['ok'])
        self.assertTrue(result['cleanup']['restored'])

    def test_missing_worker_event_evidence_cannot_report_success(self):
        reply = self.good(); del reply['event_evidence']
        result = self.run_guardian(lambda *_: reply)
        self.assertFalse(result['ok'])
        self.assertTrue(result['cleanup']['restored'])
        self.assertIsNone(result['event_evidence']['registrations_removed'])

    def test_worker_timeout_keeps_event_cleanup_unknown_and_no_submission(self):
        def fail(*_): raise supervisor.WorkerFailure('TIMEOUT', 'owned timeout')
        result = self.run_guardian(fail)
        self.assertFalse(result['ok'])
        self.assertIn('event_evidence', result)
        self.assertIsNone(result['event_evidence']['owner_exited'])
        self.assertFalse(result['error']['submission_started'])
        self.assertTrue(result['error']['outcome_unknown'])

    def test_pending_cleanup_or_missing_monitor_cannot_succeed(self):
        for change in ('pending','no_monitor','invalid_flag','short_timeout','extra_payload'):
            reply = self.good()
            if change == 'pending': reply['event_evidence']['cleanup_pending'] = True
            elif change == 'no_monitor': del reply['evidence']
            elif change == 'invalid_flag': reply['event_evidence']['owner_exited'] = 1
            elif change == 'short_timeout': reply['result']['armed_window_ms'] = 999
            else: reply['event_evidence']['private_text'] = 'synthetic must not leak'
            result = self.run_guardian(lambda *_: reply)
            self.assertFalse(result['ok'], change)
            self.assertNotIn('synthetic must not leak', json.dumps(result))

    def test_worker_failure_keeps_known_event_cleanup_flags(self):
        reply = self.good(); reply.update(ok=False,error={'code':'observation_budget_insufficient','submission_started':False})
        result = self.run_guardian(lambda *_: reply)
        self.assertFalse(result['ok'])
        self.assertEqual(result.get('event_evidence'), event_evidence())

    def test_failed_listener_cleanup_is_unknown_even_without_worker_error_flag(self):
        reply = self.good(); reply.update(ok=False,error={'code':'event_cleanup_pending','submission_started':False})
        reply['event_evidence'].update(cleanup_pending=True,owner_exited=False,cleanup_error_code='event_cleanup_pending')
        result = self.run_guardian(lambda *_: reply)
        self.assertTrue(result['error']['outcome_unknown'])
        self.assertFalse(result['error']['submission_started'])

    def test_gate_readback_must_match_this_lease_original_and_errors_must_be_empty_list(self):
        actual_restore = supervisor.GateLease.restore
        for key, invalid in (('observed',None),('observed',True),('observed',1),('errors',None)):
            def altered(lease):
                value = actual_restore(lease); value[key] = invalid
                return value
            with patch.object(supervisor.GateLease,'restore',new=altered):
                result = self.run_guardian(lambda *_: self.good())
            self.assertFalse(result['ok'], (key, invalid))

    def test_already_enabled_gate_is_restored_to_one_without_disabling_it(self):
        self.backend.value = 1
        result = self.run_guardian(lambda *_: self.good())
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['cleanup']['observed'],1)
        self.assertEqual(self.backend.writes,[])


class HintPassiveWorkerTests(unittest.TestCase):
    def invoke(self, *, failure=False, monitor_failure=False, background=True):
        calls = []; evidence = event_evidence()
        def hint(target, timeout_seconds, deadline, *, check_background):
            self.assertEqual(timeout_seconds, 1); self.assertEqual(deadline, 12345.0)
            check_background(); calls.append('hint')
            if failure:
                exc = AdapterError('event_cleanup_pending'); exc.event_evidence = evidence; exc.outcome_unknown=True
                raise exc
            return {'result':hint_result(),'event_evidence':evidence}
        class NoAdapter:
            def __init__(self, *args): raise AssertionError('passive hint must not construct Adapter')
        class FakeMonitor:
            def __init__(self, *args): pass
            def start(self): return self
            def stop(self):
                if monitor_failure: raise RuntimeError('synthetic private exception')
                return {'background_observation_passed':background}
        modules = {}
        for name, key, value in (('wxbg.adapter','Adapter',NoAdapter),('wxbg.monitor','Monitor',FakeMonitor),
                                  ('wxbg.ui_hint','wait_for_ui_hint',hint)):
            module = types.ModuleType(name); setattr(module,key,value); modules[name] = module
        request = {'action':'wait_for_ui_hint','args':{'timeout_seconds':1},
                   'target':{'pid':123,'hwnd':456,'created':1.0},'deadline':12345.0}
        out = io.StringIO()
        with patch.dict(sys.modules,modules), patch.object(worker,'_check_hint_background',create=True,side_effect=lambda _:calls.append('check')), \
             patch.object(sys,'stdin',io.StringIO(json.dumps(request))), redirect_stdout(out):
            worker.main()
        response = json.loads(out.getvalue())
        self.assertIn('hint',calls)
        self.assertEqual(response.get('event_evidence'),evidence)
        self.assertNotIn('synthetic private exception',out.getvalue())
        return response

    def test_passive_wait_never_constructs_chat_adapter(self):
        self.assertTrue(self.invoke()['ok'])

    def test_listener_failure_preserves_evidence_and_no_send(self):
        response = self.invoke(failure=True)
        self.assertFalse(response['ok']); self.assertFalse(response['error']['submission_started'])
        self.assertTrue(response['error']['outcome_unknown'])

    def test_monitor_failure_keeps_already_known_event_cleanup(self):
        self.assertFalse(self.invoke(monitor_failure=True)['ok'])

    def test_background_failure_does_not_discard_event_cleanup(self):
        self.assertFalse(self.invoke(background=False)['ok'])


if __name__ == '__main__': unittest.main()
