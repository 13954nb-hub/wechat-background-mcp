"""Real direct-interpreter Job tests against a temporary owned worker package."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from wxbg import supervisor

CHILD = '''import importlib.util,json,os,sys,time
from pathlib import Path
request=json.loads(sys.stdin.read())
import win32gui,pywintypes,psutil
value={'pid':os.getpid(),'created':psutil.Process().create_time(),
       'executable':sys.executable,'isolated':sys.flags.isolated,
       'no_site':sys.flags.no_site,'utf8_mode':sys.flags.utf8_mode,
       'win32gui':win32gui.__file__,'pywintypes':pywintypes.__file__,
       'mcp':importlib.util.find_spec('mcp').origin,
       'psutil':psutil.__file__,'stdin_received':True}
if request['args'].get('identity_path'):
    Path(request['args']['identity_path']).write_text(json.dumps(value),encoding='utf-8')
if request['args'].get('hang'): time.sleep(20)
print(json.dumps({'ok':True,'result':value}),flush=True)
'''


@unittest.skipUnless(os.name=='nt','Windows-only interpreter Job ownership')
class WorkerLaunchTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(supervisor,'_worker_command'),'direct interpreter launcher is missing')
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); package=self.root/'src/wxbg'; package.mkdir(parents=True)
        (package/'__init__.py').write_text('',encoding='utf-8')
        (package/'worker.py').write_text(CHILD,encoding='utf-8')
        self.fake_module=package/'supervisor.py'; self.fake_module.write_text('',encoding='utf-8')

    def run_owned(self, hang=False):
        import win32job
        import win32event
        parent=self; original_job=supervisor._WorkerJob; held={}
        class ProbeJob(original_job):
            def attach(self,process):
                # Deliberately let an interpreter start before guardian attach.
                time.sleep(.4)
                super().attach(process)
                held['process']=process
                held['membership']=bool(win32job.IsProcessInJob(process._handle,self.handle))
        identity=self.root/'identity.json'
        request={'action':'owned_only','args':{'hang':hang,'identity_path':str(identity)}}
        with patch.object(supervisor,'__file__',str(self.fake_module)), patch.object(supervisor,'_WorkerJob',ProbeJob):
            if hang:
                with self.assertRaises(supervisor.WorkerFailure) as caught:
                    supervisor._run_worker(request,2.0)
                self.assertEqual(caught.exception.code,'TIMEOUT')
                self.assertEqual(caught.exception.cleanup_errors,[])
                data=json.loads(identity.read_text(encoding='utf-8'))
            else:
                data=supervisor._run_worker(request,4.0)['result']
        child=held['process']
        self.assertEqual(data['pid'],child.pid)
        self.assertTrue(held['membership'])
        self.assertEqual(win32event.WaitForSingleObject(child._handle,1000),0)
        self.assertTrue(data['stdin_received'])
        self.assertEqual((data['isolated'],data['no_site'],data['utf8_mode']),(1,1,1))
        self.assertEqual(Path(data['executable']).resolve(),Path(sys._base_executable).resolve())
        for key in ('win32gui','pywintypes','mcp','psutil'):
            self.assertTrue(Path(data[key]).resolve().is_relative_to(Path(sys.prefix).resolve()),(key,data[key]))
        return data

    def test_normal_worker_is_actual_job_member_and_uses_only_venv_dependencies(self):
        self.run_owned()

    def test_timeout_reaps_actual_interpreter_after_delayed_attach(self):
        self.run_owned(hang=True)

    def test_command_isolated_and_module_paths_come_from_current_installation(self):
        command=supervisor._worker_command()
        self.assertEqual(Path(command[0]).resolve(),Path(sys._base_executable).resolve())
        self.assertEqual(command[1:6],['-I','-S','-X','utf8','-c'])
        self.assertEqual(Path(command[-2]).resolve(),(Path(sys.prefix)/'Lib/site-packages').resolve())
        self.assertEqual(Path(command[-1]).resolve(),Path(supervisor.__file__).resolve().parents[1])


if __name__=='__main__': unittest.main()
