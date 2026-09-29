"""Private image evidence must survive real worker/guardian error envelopes."""
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

from wxbg import worker, supervisor
from wxbg.policy import AdapterError
from worker_dpi_stub import worker_monitor_module
from test_gate import FakeBackend
from test_file_card_integration import _background_evidence


def trace():
    return {'version':1,'baseline':{'row_count':7,'runtime_sha256':'a'*64,'semantic_sha256':'b'*64},
            'selection_frames':[],'final_frames':[]}


class ImageEvidenceIntegrationTests(unittest.TestCase):
    def worker_reply(self,evidence,action='probe_image_entry'):
        class Adapter:
            submission_started=False
            native_evidence=None
            navigation_evidence=None
            def __init__(self,target):pass
        class Monitor:
            def __init__(self,*args,phase_recorder=None):
                if phase_recorder is None:
                    raise AssertionError('image worker must attach phase recorder')
            def start(self):return self
            def stop(self):return _background_evidence()
        def probe(adapter,**kwargs):
            adapter.image_evidence=deepcopy(evidence)
            adapter.submission_started=True
            raise AdapterError('outcome_unknown',json.dumps({'phase':'selection_draft','reason_code':'image_entry_observation_unstable','native_selections':1,'send_calls':0}))
        modules={}
        for name,attributes in [('wxbg.adapter',{'Adapter':Adapter}),('wxbg.monitor',{'Monitor':Monitor}),('wxbg.image_entry_probe',{'run':probe}),('wxbg.image_actions',{'run':probe})]:
            module=types.ModuleType(name)
            for key,value in attributes.items():setattr(module,key,value)
            modules[name]=module
        modules['wxbg.monitor']=worker_monitor_module(Monitor)
        request={'action':action,'args':{'session_ref':'a'*32,'file':{}},'target':{'pid':1,'created':1.0,'hwnd':2},'deadline':100.0}
        out=io.StringIO()
        with patch.dict(sys.modules,modules),patch.object(sys,'stdin',io.StringIO(json.dumps(request))),redirect_stdout(out):worker.main()
        return json.loads(out.getvalue())

    def test_actual_worker_and_guardian_keep_valid_trace_on_unknown(self):
        reply=self.worker_reply(trace())
        self.assertFalse(reply['ok'])
        self.assertTrue(reply['image_evidence_valid'])
        self.assertEqual(reply['image_evidence'],trace())
        with tempfile.TemporaryDirectory() as folder:
            backend=FakeBackend()
            result=supervisor.run_request({'action':'probe_image_entry','args':{}},state_dir=Path(folder),backend=backend,
                worker_runner=lambda *_:reply,mutex_factory=lambda *_:nullcontext())
        self.assertTrue(result['image_evidence_valid'])
        self.assertEqual(result['image_evidence'],trace())
        self.assertEqual(result['error']['code'],'outcome_unknown')
        self.assertTrue(result['error']['outcome_unknown'])
        self.assertEqual(backend.writes,[1,0])

    def test_invalid_worker_trace_cannot_escape_or_become_valid(self):
        invalid=trace();invalid['raw']='private body'
        reply=self.worker_reply(invalid)
        self.assertFalse(reply['image_evidence_valid'])
        self.assertNotIn('private body',json.dumps(reply))
        with tempfile.TemporaryDirectory() as folder:
            result=supervisor.run_request({'action':'probe_image_entry','args':{}},state_dir=Path(folder),backend=FakeBackend(),
                worker_runner=lambda *_:reply,mutex_factory=lambda *_:nullcontext())
        self.assertFalse(result['image_evidence_valid'])
        self.assertNotIn('private body',json.dumps(result))

    def test_public_send_image_keeps_trace_through_worker_and_supervisor(self):
        reply=self.worker_reply(trace(),action='send_image')
        self.assertTrue(reply['image_evidence_valid'])
        with tempfile.TemporaryDirectory() as folder:
            backend=FakeBackend()
            result=supervisor.run_request({'action':'send_image','args':{}},state_dir=Path(folder),backend=backend,
                worker_runner=lambda *_:reply,mutex_factory=lambda *_:nullcontext())
        self.assertTrue(result['image_evidence_valid'])
        self.assertEqual(result['image_evidence'],trace())
        self.assertEqual(result['error']['code'],'outcome_unknown')
        self.assertEqual(backend.writes,[1,0])


if __name__=='__main__':unittest.main()
