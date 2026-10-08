import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real agy is terminated in tests)
import broker as broker_mod
from agy_park import Parker, ParkError, resume_command
from quota import QuotaError

CONV = '6d0d01dc-041e-496e-a521-cae0b105900c'


class FakeIO:
    def __init__(self):
        self.procs = {101: (['agy', '--dangerously-skip-permissions'], '/w/project')}
        self.running = {101}
        self.quits = True
        self.conversations = {101: (True, CONV)}
        self.saved = {CONV}
        self.killed, self.log = [], []

    def argv(self, pid):
        return self.procs.get(pid, (None, None))[0]

    def cwd(self, pid):
        return self.procs.get(pid, (None, None))[1]

    def terminate(self, pid):
        self.killed.append(pid)
        self.log.append(('terminate', pid))
        if self.quits:
            self.running.discard(pid)

    def alive(self, pid):
        return pid in self.running

    def conversation(self, pid):
        return self.conversations.get(pid, (False, None))

    def conversation_saved(self, conversation):
        return conversation in self.saved

    def sleep(self, seconds):
        pass


class ParkerTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.p = Parker(self.io)

    def test_plan_reads_folder_and_conversation_before_quitting(self):
        [item] = self.p.plan([101])
        self.assertEqual((item['cwd'], item['conversation'], self.io.killed), ('/w/project', CONV, []))

    def test_unreadable_process_refuses(self):
        self.io.procs = {}
        with self.assertRaises(ParkError):
            self.p.plan([101])

    def test_pruned_conversation_is_never_quit(self):
        # agy keeps its latest 500 conversations; quitting a session whose file is gone would lose it for good.
        self.io.saved = set()
        with self.assertRaises(ParkError):
            self.p.plan([101])
        self.io.conversations = {}
        with self.assertRaises(ParkError):  # its log cannot be found
            self.p.plan([101])

    def test_session_that_will_not_quit_is_still_reported(self):
        self.io.quits = False
        parked = []
        with self.assertRaises(ParkError):
            self.p.park(self.p.plan([101]), parked)
        self.assertEqual([i['pid'] for i in parked], [101])

    def test_command_continues_the_same_conversation(self):
        parked = []
        self.p.park(self.p.plan([101]), parked)
        [item] = self.p.commands(parked)
        self.assertEqual(item['command'], f'agy --dangerously-skip-permissions --conversation={CONV}')

    def test_session_without_a_conversation_simply_starts_again(self):
        self.io.conversations = {101: (True, None)}  # never got a message: nothing to lose or resume
        parked = []
        self.p.park(self.p.plan([101]), parked)
        self.assertEqual(self.p.commands(parked)[0]['command'], 'agy --dangerously-skip-permissions')

    def test_resume_command_drops_an_earlier_resume_choice(self):
        self.assertEqual(resume_command(['agy', '-c', '--model', 'x'], CONV), f'agy --model x --conversation={CONV}')
        self.assertEqual(resume_command(['agy', '--conversation', 'old', '--conversation=older'], CONV),
                         f'agy --conversation={CONV}')


class BrokerParkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict('os.environ', {'AGY_ROTATION_LOCK': self.tmp.name + '/rotation.lock',
                                        'AGY_OWNED_DIR': self.tmp.name + '/owned'})
        env.start()
        self.addCleanup(env.stop)
        self.io = FakeIO()
        self.active = ['A']
        profiles = types.SimpleNamespace(list=lambda: {'A': {}, 'B': {}}, read=lambda label: None,
                                         active=lambda: self.active[0])
        self.b = broker_mod.Broker(self.tmp.name, profiles=profiles, helper='/nonexistent', parker=Parker(self.io))
        self.results = []
        self.b.on_park = self.results.extend
        for name, value in (('unowned_agy', [(101, False)]), ('working', [])):
            p = patch.object(broker_mod, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def helper(self, ok=True):
        def run(args, **kw):
            self.io.log.append(('helper', 101 in self.io.running))
            if not ok:
                return types.SimpleNamespace(returncode=1, stdout=b'', stderr=b'ERROR: boom')
            self.active[0] = 'B'
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {'activation': 'OK', 'selected': 'B'}).encode(), stderr=b'')
        return patch.object(broker_mod.subprocess, 'run', side_effect=run)

    def test_idle_session_is_closed_before_the_switch_and_reported(self):
        with self.helper():
            r = self.b.switch('B', close_all=True)
        self.assertEqual(r['selected'], 'B')
        self.assertEqual(self.io.log, [('terminate', 101), ('helper', False)])  # agy gone before the account changes
        self.assertEqual([i['command'] for i in self.results], [f'agy --dangerously-skip-permissions --conversation={CONV}'])

    def test_closed_session_is_reported_even_when_the_switch_fails(self):
        with self.helper(ok=False), self.assertRaises(QuotaError):
            self.b.switch('B', close_all=True)
        self.assertEqual([i['pid'] for i in self.results], [101])

    def test_busy_session_is_never_touched(self):
        with patch.object(broker_mod, 'working', return_value=['child:202']), self.helper():
            with self.assertRaises(QuotaError) as e:
                self.b.switch('B', close_all=True)
        self.assertEqual(str(e.exception), 'AGY_WORKING_NOT_SWITCHED')
        self.assertEqual(self.io.log, [])

    def test_pruned_conversation_refuses_without_touching_it(self):
        self.io.saved = set()  # agy already pruned it: quitting would lose it
        with self.helper(), self.assertRaises(QuotaError) as e:
            self.b.switch('B', close_all=True)
        self.assertEqual(str(e.exception), 'AGY_CONVERSATION_PRUNED_NOT_SWITCHED')
        self.assertEqual(self.io.log, [])

    def test_unreadable_session_refuses_without_touching_it(self):
        self.io.procs = {}
        with self.helper(), self.assertRaises(QuotaError) as e:
            self.b.switch('B', close_all=True)
        self.assertEqual(str(e.exception), 'AGY_UNREADABLE_NOT_SWITCHED')
        self.assertEqual(self.io.log, [])

    def test_every_reason_counts_as_not_switched(self):
        for code in broker_mod.AGY_REFUSALS:
            self.assertIn(code, broker_mod.NOT_SWITCHED)
            self.assertEqual(broker_mod.RETRY_AFTER[code], 120)


class ForcedSwitchTests(BrokerParkTests):
    """The live account is used up: switch even past sessions that cannot be closed safely."""

    def args(self):
        seen = []

        def run(args, **kw):
            seen.append(args)
            self.active[0] = 'B'
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {'activation': 'OK', 'selected': 'B'}).encode(), stderr=b'')
        return seen, patch.object(broker_mod.subprocess, 'run', side_effect=run)

    def test_unsafe_session_no_longer_blocks_and_is_left_alone(self):
        self.io.saved = set()
        seen, p = self.args()
        with p:
            self.assertEqual(self.b.switch('B', close_all=True, force=True)['selected'], 'B')
        self.assertIn('--force', seen[0])
        self.assertEqual(self.io.killed, [])

    def test_working_session_is_not_quit_even_then(self):
        seen, p = self.args()
        with patch.object(broker_mod, 'working', return_value=['child:202']), p:
            self.b.switch('B', close_all=True, force=True)
        self.assertEqual(self.io.killed, [])
        self.assertIn('--force', seen[0])

    def test_idle_session_is_still_closed_and_reported(self):
        seen, p = self.args()
        with p:
            self.b.switch('B', close_all=True, force=True)
        self.assertEqual((self.io.killed, [i['pid'] for i in self.results]), ([101], [101]))

    def test_without_force_nothing_changes(self):
        self.io.saved = set()
        seen, p = self.args()
        with p, self.assertRaises(QuotaError):
            self.b.switch('B', close_all=True)
        self.assertEqual(seen, [])

if __name__ == '__main__':
    unittest.main()
