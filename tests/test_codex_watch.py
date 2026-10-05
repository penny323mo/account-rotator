import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from quota import QuotaError
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import Service
from test_service import row

TID = '01a0aa3c-1e93-79e1-89ea-c83e3001e106'


class FakeWatchIO:
    def __init__(self):
        self.state = {'last': 'task_started', 'message': None, 'path': '/x/rollout.jsonl'}
        self.alive, self.busy, self.released = True, True, []

    def rollout(self, thread):
        return self.state['path']

    def turn(self, path, start=0):
        if start >= self.state.get('size', 0) + 1 and self.state.get('stale'):
            return None, None  # nothing written since the resume started
        return self.state['last'], self.state['message']

    def size(self, path):
        return self.state.get('size', 0) + 1

    def cli_alive(self, thread):
        return self.alive

    def cli_busy(self, target):
        return self.busy

    def release(self, target):
        self.released.append(target)
        self.alive = False


class CodexWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.io = FakeWatchIO()
        self.notes = []
        self.s = self.service()

    def service(self):
        profiles = Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A'))
        client = Mock(fetch=Mock(side_effect=lambda label: row(label)))
        codex = Mock(quota=Mock(return_value={'profiles': {}, 'active': None}))
        return Service(self.temp.name, profiles, client, Mock(), Mock(return_value=[]), codex=codex,
                       notifier=self.notes.append, watch_io=self.io)

    def add(self):
        return self.s.call('codex.watch.add', {'thread': TID, 'cwd': '/Users/u/my-project', 'tmux': 'codex-continue:codex-01e106'})

    def watch(self):
        return self.s.status()['codex_watches'][-1]

    def test_add_records_running_watch(self):
        self.add()
        w = self.watch()
        self.assertEqual((w['thread'], w['project'], w['status']), (TID, 'my-project', 'running'))
        self.assertEqual(self.s.events[-1]['kind'], 'codex_continue_started')

    def test_rejects_bad_thread_id(self):
        with self.assertRaises(QuotaError):
            self.s.call('codex.watch.add', {'thread': '../../etc', 'cwd': '/x', 'tmux': 'a:b'})

    def test_previous_turn_is_not_taken_for_the_resumed_one(self):
        # 2026-10-04 21:31: the usage-limit turn had just ended (task_complete, no message); the resumed CLI
        # was still starting, and the watch sent Esc + Ctrl-C twice, killing it before "continue" ran.
        self.io.state.update(last='task_complete', message=None, stale=True)
        self.io.busy = False
        self.add()
        self.s.check_codex_watches()
        self.assertEqual(self.watch()['status'], 'running')
        self.assertEqual(self.io.released, [])
        self.io.state.update(stale=False, size=50, message='DONE')  # the resumed turn finished
        self.s.check_codex_watches()
        self.assertEqual((self.watch()['status'], self.watch()['summary']), ('done', 'DONE'))

    def test_still_working_stays_running(self):
        self.add()
        self.io.state['last'] = 'task_complete'  # main turn done but sub-agents still working
        self.s.check_codex_watches()
        self.assertEqual(self.watch()['status'], 'running')
        self.assertEqual(self.io.released, [])

    def test_done_releases_cli_notifies_and_records(self):
        self.add()
        self.io.state.update(last='task_complete', message='PARTIAL — `STOP = HOST_GUARD`\n\ndetails')
        self.io.busy = False
        self.s.check_codex_watches()
        w = self.watch()
        self.assertEqual(w['status'], 'done')
        self.assertEqual(w['summary'], 'PARTIAL — `STOP = HOST_GUARD`')
        self.assertTrue(w['released'])
        self.assertEqual(self.io.released, ['codex-continue:codex-01e106'])
        self.assertIn('my-project', self.notes[-1])
        self.assertEqual(self.s.events[-1]['kind'], 'codex_continue_done')

    def test_cli_gone_before_completion_is_interrupted(self):
        self.add()
        self.io.alive = False
        self.s.check_codex_watches()
        self.assertEqual(self.watch()['status'], 'interrupted')
        self.assertEqual(self.s.events[-1]['kind'], 'codex_continue_interrupted')
        self.assertTrue(self.notes)

    def test_finished_watch_is_not_reprocessed(self):
        self.add()
        self.io.alive = False
        self.s.check_codex_watches()
        count = len(self.notes)
        self.s.check_codex_watches()
        self.assertEqual(len(self.notes), count)

    def test_codex_events_are_kept_apart_from_gemini_noise(self):
        self.add()
        for _ in range(12):
            self.s.event('config_changed', enabled=False)
        kinds = [e['kind'] for e in self.s.status()['codex_events']]
        self.assertIn('codex_continue_started', kinds)
        self.assertNotIn('config_changed', kinds)

    def test_gemini_events_exclude_codex(self):
        self.s.event('switched', selected='GEMINI_B', source='manual')
        self.add()
        for _ in range(12):
            self.s.event('codex_auto_changed', enabled=True)
        kinds = [e['kind'] for e in self.s.status()['gemini_events']]
        self.assertIn('switched', kinds)
        self.assertFalse(any(k.startswith('codex_') for k in kinds))

    def test_watches_survive_restart(self):
        self.add()
        self.assertEqual(self.service().status()['codex_watches'][-1]['status'], 'running')


if __name__ == '__main__':
    unittest.main()
