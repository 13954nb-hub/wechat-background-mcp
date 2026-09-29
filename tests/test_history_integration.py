"""Actual MCP validation plus isolated worker/guardian contracts; no Weixin."""
import asyncio
from contextlib import nullcontext, redirect_stdout
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
from wxbg.journal import Journal
from test_history_contract import evidence, summary, ARGS
from test_gateway import load_isolated_gateway
from test_gate import FakeBackend
from worker_dpi_stub import worker_monitor_module


class HistoryGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.gateway=load_isolated_gateway(Path(self.temp.name))
        self.assertTrue(hasattr(self.gateway,'wechat_scroll_messages'))

    def test_actual_mcp_schema_and_invalid_args_never_dispatch(self):
        tool=next(t for t in asyncio.run(self.gateway.SERVER.list_tools()) if t.name=='wechat_scroll_messages')
        self.assertFalse(tool.annotations.readOnlyHint);self.assertFalse(tool.annotations.idempotentHint)
        self.assertIs(tool.inputSchema['additionalProperties'],False)
        prop=tool.inputSchema['properties']['steps']
        self.assertEqual((prop['type'],prop['minimum'],prop['maximum']),('integer',1,4))
        base={**ARGS,'operation_id':'owned-test'}
        for key,bad in [('steps',v) for v in (True,1.0,'1',0,5)]+[('direction','up'),('filter',{}),('restore',True)]:
            with patch.object(self.gateway,'execute') as dispatch,self.assertRaises(ToolError):
                asyncio.run(self.gateway.SERVER.call_tool('wechat_scroll_messages',{**base,key:bad}))
            dispatch.assert_not_called()

    def test_direct_invalid_args_never_dispatch(self):
        for key,bad in (('steps',True),('direction','up'),('session_ref','')):
            with patch.object(self.gateway,'execute') as dispatch,self.assertRaises(ToolError):
                self.gateway.wechat_scroll_messages(**{**ARGS,'operation_id':'owned-test',key:bad})
            dispatch.assert_not_called()

    def test_replay_reuses_summary_without_reexecuting_relative_input(self):
        reply={'ok':True,'result':summary(),'history_evidence':evidence()}
        with patch.object(self.gateway,'execute',return_value=reply) as dispatch:
            first=self.gateway.wechat_scroll_messages(**ARGS,operation_id='owned-history-one')
            second=self.gateway.wechat_scroll_messages(**ARGS,operation_id='owned-history-one')
        dispatch.assert_called_once_with('scroll_messages',ARGS)
        self.assertTrue(first['ok']);self.assertTrue(second['replayed'])
        self.assertEqual(second['result'],summary())

    def test_unknown_outcome_is_not_retried(self):
        reply={'ok':False,'worker_started':True,'error':{'code':'TIMEOUT','outcome_unknown':True}}
        with patch.object(self.gateway,'execute',return_value=reply) as dispatch:
            for _ in range(2):
                with self.assertRaises(ToolError):
                    self.gateway.wechat_scroll_messages(**ARGS,operation_id='owned-history-timeout')
        self.assertEqual(dispatch.call_count,1)

    def test_proven_pre_wheel_row_error_is_journaled_as_rejected(self):
        history=evidence()
        history.update(delivery_started=False,completed_steps=0,
                       viewport_settled=False,conversation_preserved=False,
                       draft_preserved=False,viewport_changed=None,
                       primary_error_code='history_row_unsupported')
        reply={'ok':False,'worker_started':True,
               'error':{'code':'history_row_unsupported','outcome_unknown':False,
                        'navigation_started':False,'submission_started':False},
               'history_evidence':history,
               'evidence':{'background_observation_passed':True},
               'cleanup':{'status':'restored','restored':True,'observed':0,
                          'journal_committed':True,'errors':[]}}
        with patch.object(self.gateway,'execute',return_value=reply) as dispatch:
            with self.assertRaises(ToolError) as caught:
                self.gateway.wechat_scroll_messages(**ARGS,operation_id='row-pre-wheel')
            self.assertEqual(json.loads(str(caught.exception))['error']['outcome_unknown'],False)
            with self.assertRaises(ToolError):
                self.gateway.wechat_scroll_messages(**ARGS,operation_id='row-pre-wheel')
        dispatch.assert_called_once_with('scroll_messages',ARGS)
        operation=Journal(Path(self.temp.name)/'operations.sqlite3').get('row-pre-wheel')
        self.assertEqual(operation['state'],'rejected')
        self.assertEqual(operation['reason_code'],'history_row_unsupported')

    def test_pre_wheel_rejection_requires_complete_consistent_evidence(self):
        history=evidence()
        history.update(delivery_started=False,completed_steps=0,
                       viewport_settled=False,conversation_preserved=False,
                       draft_preserved=False,viewport_changed=None,
                       primary_error_code='history_row_unsupported')
        good={'ok':False,'worker_started':True,
              'error':{'code':'history_row_unsupported','outcome_unknown':False,
                       'navigation_started':False,'submission_started':False},
              'history_evidence':history,
              'evidence':{'background_observation_passed':True},
              'cleanup':{'status':'restored','restored':True,'observed':0,
                         'journal_committed':True,'errors':[]}}
        for label, mutate in (
                ('missing_history',lambda r:r.pop('history_evidence')),
                ('delivered',lambda r:r['history_evidence'].__setitem__('delivery_started',True)),
                ('bad_code',lambda r:r['history_evidence'].__setitem__('primary_error_code','draft_conflict')),
                ('background',lambda r:r['evidence'].__setitem__('background_observation_passed',False)),
                ('gate',lambda r:r['cleanup'].__setitem__('restored',False)),
                ('navigation',lambda r:r['error'].__setitem__('navigation_started',True)),
                ('bad_error_code',lambda r:r['error'].__setitem__('code',['history_row_unsupported']))):
            with self.subTest(label=label):
                import copy
                reply=copy.deepcopy(good);mutate(reply)
                with patch.object(self.gateway,'execute',return_value=reply):
                    with self.assertRaises(ToolError):
                        self.gateway.wechat_scroll_messages(**ARGS,operation_id='bad-'+label)
                self.assertEqual(Journal(Path(self.temp.name)/'operations.sqlite3').get('bad-'+label)['state'],
                                 'outcome_unknown')


class HistoryGuardianTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name);self.backend=FakeBackend();self.backend.state_dir=self.directory
    def run_guardian(self,runner):
        return supervisor.run_request({'action':'scroll_messages','args':ARGS},state_dir=self.directory,
            backend=self.backend,worker_runner=runner,mutex_factory=lambda *_:nullcontext())
    def good(self):
        return {'ok':True,'result':summary(),'history_evidence':evidence(),'evidence':{'background_observation_passed':True}}

    def test_success_checks_independent_background_and_gate(self):
        value=self.run_guardian(lambda *_:self.good())
        self.assertTrue(value['ok']);self.assertEqual(value['history_evidence'],evidence())
        self.assertTrue(value['cleanup']['restored'])

    def test_missing_or_malformed_evidence_cannot_succeed(self):
        for alteration in ('missing','extra','false_settle','missing_monitor','bad_count'):
            reply=self.good()
            if alteration=='missing':del reply['history_evidence']
            elif alteration=='extra':reply['history_evidence']['private_text']='must not leak'
            elif alteration=='false_settle':reply['history_evidence']['viewport_settled']=False
            elif alteration=='missing_monitor':del reply['evidence']
            else:reply['result']['counts']['completed_steps']=0
            value=self.run_guardian(lambda *_:reply)
            self.assertFalse(value['ok'],alteration);self.assertNotIn('result',value)
            self.assertTrue(value['error']['outcome_unknown']);self.assertNotIn('must not leak',json.dumps(value))

    def test_timeout_navigation_unknown_but_gate_known_restored(self):
        def fail(*_):raise supervisor.WorkerFailure('TIMEOUT','owned timeout')
        value=self.run_guardian(fail)
        self.assertFalse(value['ok']);self.assertTrue(value['cleanup']['restored'])
        self.assertIsNone(value['history_evidence']['delivery_started'])
        self.assertIsNone(value['error']['navigation_started'])
        self.assertFalse(value['error']['submission_started']);self.assertTrue(value['error']['outcome_unknown'])

    def test_failed_operation_after_delivery_is_unknown(self):
        reply=self.good();reply.update(ok=False,error={'code':'history_view_not_settled'})
        reply['history_evidence'].update(viewport_settled=False,primary_error_code='history_view_not_settled')
        value=self.run_guardian(lambda *_:reply)
        self.assertFalse(value['ok']);self.assertTrue(value['error']['navigation_started'])
        self.assertTrue(value['error']['outcome_unknown']);self.assertFalse(value['error']['submission_started'])

    def test_failed_operation_before_delivery_remains_known_not_submitted(self):
        reply=self.good();reply.update(ok=False,error={'code':'draft_conflict'})
        reply['history_evidence'].update(delivery_started=False,completed_steps=0,viewport_settled=False,primary_error_code='draft_conflict')
        value=self.run_guardian(lambda *_:reply)
        self.assertFalse(value['error']['navigation_started']);self.assertFalse(value['error']['outcome_unknown'])
        self.assertFalse(value['error']['submission_started'])

    def test_original_gate_one_preserved(self):
        self.backend.value=1
        value=self.run_guardian(lambda *_:self.good())
        self.assertTrue(value['ok']);self.assertEqual(value['cleanup']['observed'],1);self.assertEqual(self.backend.writes,[])

    def test_contradictory_delivery_flags_cannot_claim_known_no_input(self):
        reply=self.good();reply.update(ok=False,error={'code':'history_view_not_settled'})
        reply['history_evidence'].update(delivery_started=False,completed_steps=1,viewport_settled=False,
                                        primary_error_code='history_view_not_settled')
        value=self.run_guardian(lambda *_:reply)
        self.assertTrue(value['error']['outcome_unknown'])
        self.assertEqual(value['history_evidence']['primary_error_code'],'history_evidence_invalid')

    def test_gate_readback_exact_and_no_cleanup_errors(self):
        restore=supervisor.GateLease.restore
        for key,bad in (('observed',None),('observed',True),('observed',1),('errors',None)):
            def alter(lease):
                value=restore(lease);value[key]=bad;return value
            with patch.object(supervisor.GateLease,'restore',new=alter):
                value=self.run_guardian(lambda *_:self.good())
            self.assertFalse(value['ok']);self.assertTrue(value['error']['outcome_unknown'])


class HistoryWorkerTests(unittest.TestCase):
    def invoke(self,fail=False,monitor_failure=False,background=True):
        calls=[]
        class FakeAdapter:
            submission_started=False
            def __init__(self,*_):pass
            def dispatch(self,*_):raise AssertionError('history must receive guardian deadline')
        def scroll(adapter,session_ref,direction,steps,deadline):
            calls.append(deadline);adapter.history_evidence=evidence();adapter.navigation_started=True
            if fail:
                exc=AdapterError('history_view_not_settled');exc.outcome_unknown=True;raise exc
            return summary()
        class FakeMonitor:
            def __init__(self,*_):pass
            def start(self):return self
            def stop(self):
                if monitor_failure:raise RuntimeError('synthetic private exception')
                return {'background_observation_passed':background}
        modules={}
        for name,key,value in (('wxbg.adapter','Adapter',FakeAdapter),('wxbg.monitor','Monitor',FakeMonitor),
                               ('wxbg.history_actions','scroll_messages',scroll)):
            module=types.ModuleType(name);setattr(module,key,value);modules[name]=module
        modules['wxbg.monitor']=worker_monitor_module(FakeMonitor)
        request={'action':'scroll_messages','args':ARGS,'target':{'pid':1,'hwnd':2,'created':3},'deadline':12345.0}
        out=io.StringIO()
        with patch.dict(sys.modules,modules),patch.object(sys,'stdin',io.StringIO(json.dumps(request))),redirect_stdout(out):
            worker.main()
        response=json.loads(out.getvalue())
        self.assertEqual(calls,[12345.0]);self.assertEqual(response['history_evidence'],evidence())
        self.assertNotIn('synthetic private exception',out.getvalue())
        if not response['ok']:
            self.assertFalse(response['error']['submission_started']);self.assertTrue(response['error']['outcome_unknown'])
        return response
    def test_guardian_deadline_forwarded(self):self.assertTrue(self.invoke()['ok'])
    def test_failure_keeps_navigation_evidence(self):self.assertFalse(self.invoke(fail=True)['ok'])
    def test_monitor_failure_keeps_navigation_evidence(self):self.assertFalse(self.invoke(monitor_failure=True)['ok'])
    def test_background_failure_keeps_navigation_evidence(self):self.assertFalse(self.invoke(background=False)['ok'])

if __name__=='__main__':unittest.main()
