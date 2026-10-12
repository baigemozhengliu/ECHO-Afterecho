"""Real Windows process tests, using isolated hosts and no user library."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))
if os.name == 'nt':
    from lifecycle import HostProcess, echo_main_pid

@unittest.skipUnless(os.name == 'nt', 'Windows lifecycle')
class LifecycleTests(unittest.TestCase):
    def exercise(self, kill_supervisor=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # A harmless, named ECHO host waits for input; it is never the installed app.
            shutil.copyfile(Path(os.environ['SystemRoot']) / 'System32/cmd.exe', root / 'ECHO.exe')
            host = subprocess.Popen([str(root / 'ECHO.exe'), '/d', '/c', 'set /p value='],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(echo_main_pid(host.pid), host.pid)
            code = '''
import sys, subprocess, time
from pathlib import Path
from lifecycle import ServiceLifetime
life = ServiceLifetime(int(sys.argv[1]), grace_seconds=1)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[2]).write_text(str(child.pid))
while True: time.sleep(.1)
'''
            env = {**os.environ, 'PYTHONPATH': str(BRIDGE)}
            supervisor = subprocess.Popen([sys.executable, '-c', code, str(host.pid), str(root/'child')], env=env)
            child_handle = None
            try:
                deadline = time.monotonic() + 10
                while not (root/'child').exists() and supervisor.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue((root/'child').exists(), 'supervisor failed before child startup')
                child_handle = HostProcess(int((root/'child').read_text()))
                self.assertTrue(child_handle.alive())
                if kill_supervisor: supervisor.kill()
                else: host.terminate()
                supervisor.wait(timeout=5)
                deadline = time.monotonic() + 3
                while child_handle.alive() and time.monotonic() < deadline: time.sleep(.05)
                self.assertFalse(child_handle.alive(), 'descendant survived supervisor shutdown')
                if kill_supervisor: self.assertIsNone(host.poll(), 'test host must be unaffected')
            finally:
                if supervisor.poll() is None: supervisor.kill(); supervisor.wait()
                if host.poll() is None: host.terminate()
                host.wait(); host.stdin.close()
                if child_handle: child_handle.close()

    def test_host_exit_cleans_service_tree(self): self.exercise()
    def test_supervisor_crash_cleans_service_tree(self): self.exercise(True)

if __name__ == '__main__': unittest.main()
