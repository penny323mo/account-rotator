"""Real daemon/socket/CLI, entirely synthetic profiles and switcher."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import DEFAULTS, rpc

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = '''
import sys
from pathlib import Path
from service import Service, serve
from quota import stamp
root = Path(sys.argv[1])
class Profiles:
    active_label = 'A'
    def list(self): return {'A': {'id': 'a'}, 'B': {'id': 'b'}}
    def active(self): return self.active_label
class Quota:
    def fetch(self, label):
        value = 1 if label == 'A' else 90
        return {'label': label, 'status': 'OK', 'updated_at': stamp(), 'groups': [
            {'family': 'gemini', 'windows': {w: {'remaining_percent': value, 'reset_at': None}
                                            for w in ('5h', 'weekly')}}]}
profiles = Profiles()
class Broker:
    def switch(self, label, close_all=False):
        with (root/'fake-switches').open('a') as f: f.write(label + '\\n')
        profiles.active_label = label
        return {'selected': label}
def factory(root, **kwargs):
    import isolation  # the fixture daemon must not reach the real Codex / agy helpers either
    return Service(root, profiles, Quota(), Broker(), busy=lambda: [])
serve(root, service_factory=factory)
'''


class SocketTests(unittest.TestCase):
    def test_real_rpc_notify_only_off_refresh_conflict_and_manual_use(self):
        with tempfile.TemporaryDirectory(prefix='agy-rpc-', dir='/tmp') as tmp:
            root = Path(tmp)
            script = root / 'fake_daemon.py'
            script.write_text(FIXTURE)
            config = {**DEFAULTS, 'participants': ['A', 'B'], 'enabled': True,
                      'mode': 'notify', 'notify': False}
            (root / 'config.json').write_text(json.dumps(config))
            env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(ROOT / 'daemon'), str(ROOT / 'tests')])}
            proc = subprocess.Popen([sys.executable, str(script), str(root)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+10
                while time.monotonic()<deadline:
                    if (root/'rotatord.sock').exists():
                        state = rpc(root, 'status')
                        if state['pending'] and state['pending']['state']=='NOTIFY_ONLY': break
                    time.sleep(.05)
                else: self.fail('fixture daemon did not recommend a rotation')
                self.assertFalse((root/'fake-switches').exists())
                cli = [sys.executable, str(ROOT/'agy-rotator'), '--home', str(root)]
                output = subprocess.check_output([*cli, 'off'])
                state = json.loads(output)
                self.assertFalse(state['config']['enabled'])
                self.assertIsNone(state['pending'])
                before = state['profiles']['A']['updated_at']
                subprocess.check_output([*cli, 'refresh'])
                deadline = time.monotonic()+3
                while time.monotonic()<deadline:
                    state = rpc(root, 'status')
                    if state['profiles']['A']['updated_at'] != before: break
                    time.sleep(.01)
                else: self.fail('refresh stopped when rotation was off')
                self.assertFalse((root/'fake-switches').exists())
                with self.assertRaisesRegex(Exception, 'CONFIG_CONFLICT'):
                    rpc(root, 'config.set', {'revision': -1, 'config': config})
                subprocess.check_output([*cli, 'use', 'B', '--now'])
                self.assertEqual((root/'fake-switches').read_text(), 'B\n')
                duplicate = subprocess.run([sys.executable, str(script), str(root)], env=env,
                                           capture_output=True, timeout=3)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn(b'DAEMON_ALREADY_RUNNING', duplicate.stderr)
                self.assertEqual(rpc(root, 'status')['active'], 'B')
            finally:
                proc.terminate()
                proc.communicate(timeout=3)


if __name__ == '__main__': unittest.main()
