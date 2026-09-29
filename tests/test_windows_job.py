"""Real Windows lifecycle smoke test; never opens Weixin or its memory."""
import os
import subprocess
import sys
import unittest
from wxbg.supervisor import _WorkerJob

@unittest.skipUnless(os.name=='nt','Windows Job API')
class WindowsJobTests(unittest.TestCase):
    def test_closing_job_terminates_only_its_owned_test_process(self):
        job=_WorkerJob()
        child=None
        try:
            child=subprocess.Popen([sys.executable,'-c','input()'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
            job.attach(child)
            self.assertIsNone(child.poll())
            job.close()
            child.wait(timeout=3)
            self.assertIsNotNone(child.returncode)
        finally:
            job.close()
            if child:
                if child.poll() is None:child.kill();child.wait(timeout=3)
                child.stdin.close()

if __name__=='__main__':unittest.main()
