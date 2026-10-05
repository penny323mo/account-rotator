import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from codex_watch import WatchIO
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import Service
from test_service import row


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.busy = Mock(return_value=[])
        self.codex_busy = Mock(return_value=[])
        profiles = Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A'))
        client = Mock(fetch=Mock(side_effect=lambda label: row(label)))
        codex = Mock(quota=Mock(return_value={'profiles': {}, 'active': None}))
        watch_io = Mock(codex_working=self.codex_busy)
        self.s = Service(self.temp.name, profiles, client, Mock(), self.busy, codex=codex, watch_io=watch_io)

    def test_idle_by_default(self):
        self.s.update_activity()
        a = self.s.status()['activity']
        self.assertEqual((a['gemini']['working'], a['codex']['working']), (False, False))

    def test_working_reported_per_provider(self):
        self.busy.return_value = ['agy_print:12']
        self.codex_busy.return_value = ['turn:01a0aa3c']
        self.s.update_activity()
        a = self.s.status()['activity']
        self.assertTrue(a['gemini']['working'])
        self.assertTrue(a['codex']['working'])
        self.assertEqual(a['gemini']['reasons'], ['agy_print:12'])

    def test_live_account_at_its_limit_shows_used_up_not_working(self):
        self.busy.return_value = ['agy_print:7']  # agy still runs, but only hits the quota wall
        self.s.rows, self.s.active = {'A': row('A', five=0)}, 'A'
        self.s.update_activity()
        g = self.s.status()['activity']['gemini']
        self.assertEqual((g['working'], g.get('exhausted')), (False, True))

    def test_short_gaps_between_jobs_do_not_flicker_to_idle(self):
        clock = [1000.0]
        with unittest.mock.patch('service.time.time', lambda: clock[0]):
            self.busy.return_value = ['agy_print:12']
            self.s.update_activity()
            self.busy.return_value = []
            clock[0] += 10
            self.s.update_activity()
            self.assertTrue(self.s.status()['activity']['gemini']['working'])
            clock[0] += 15
            self.s.update_activity()
            self.assertFalse(self.s.status()['activity']['gemini']['working'])

    def test_probe_failure_is_unknown_not_idle(self):
        self.busy.side_effect = OSError('ps failed')
        self.s.update_activity()
        self.assertIsNone(self.s.status()['activity']['gemini']['working'])


class CodexWorkingTests(unittest.TestCase):
    def write(self, name, events, age=0):
        d = Path(self.temp.name) / 'sessions' / '2026' / '09' / '27'
        d.mkdir(parents=True, exist_ok=True)
        f = d / f'rollout-2026-09-27T10-00-00-{name}.jsonl'
        f.write_text(''.join(json.dumps({'payload': {'type': e}}) + '\n' for e in events))
        t = time.time() - age
        os.utime(f, (t, t))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.io = WatchIO(self.temp.name)

    def test_open_turn_is_working(self):
        self.write('aaaa', ['task_started', 'task_complete', 'task_started'])
        self.assertEqual(self.io.codex_working(), ['turn:aaaa'])

    def test_finished_or_aborted_turns_are_idle(self):
        self.write('bbbb', ['task_started', 'task_complete'])
        self.write('cccc', ['task_started', 'turn_aborted'])
        self.assertEqual(self.io.codex_working(), [])

    def test_long_turn_with_lots_of_output_stays_working(self):
        # task_started followed by >512 KB of tool output must not look idle (seen on a 98 MB thread).
        filler = [json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': 'x' * 4000}})] * 200
        d = Path(self.temp.name) / 'sessions' / '2026' / '09' / '27'
        d.mkdir(parents=True, exist_ok=True)
        f = d / 'rollout-2026-09-27T10-00-00-long.jsonl'
        f.write_text(json.dumps({'payload': {'type': 'task_started'}}) + '\n' + '\n'.join(filler) + '\n')
        self.assertEqual(self.io.codex_working(), ['turn:long'])

    def test_appended_completion_is_picked_up_incrementally(self):
        self.write('inc', ['task_started'])
        self.assertEqual(self.io.codex_working(), ['turn:inc'])
        path = next((Path(self.temp.name) / 'sessions').rglob('*inc.jsonl'))
        with open(path, 'a') as fh:
            fh.write(json.dumps({'payload': {'type': 'task_complete'}}) + '\n')
        self.assertEqual(self.io.codex_working(), [])

    def test_old_files_are_ignored(self):
        self.write('dddd', ['task_started'], age=3 * 3600)
        self.assertEqual(self.io.codex_working(), [])


class ResumedTurnTests(unittest.TestCase):
    def test_only_turns_written_after_the_resume_count(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'rollout.jsonl'
            ev = lambda kind, msg=None: json.dumps({'type': 'event_msg', 'payload': {'type': kind, 'last_agent_message': msg}}) + '\n'
            f.write_text(ev('task_started') + ev('task_complete'))  # the turn that hit the usage limit
            io, start = WatchIO(d), f.stat().st_size
            self.assertEqual(io.turn(str(f), start), (None, None))
            self.assertEqual(io.turn(str(f))[0], 'task_complete')
            with f.open('a') as out:
                out.write(ev('task_started'))
            self.assertEqual(io.turn(str(f), start), ('task_started', None))
            with f.open('a') as out:
                out.write(ev('task_complete', 'DONE'))
            self.assertEqual(io.turn(str(f), start), ('task_complete', 'DONE'))

class ClaudeWorkingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.io = WatchIO(self.temp.name, claude_home=str(self.home / 'claude'))

    def write(self, rel, age):
        f = self.home / 'claude' / 'projects' / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{}\n')
        t = time.time() - age
        os.utime(f, (t, t))

    def test_a_session_that_wrote_in_the_last_two_minutes_is_working(self):
        self.write('proj-a/2f6571ed-c4a2.jsonl', 30)
        self.assertEqual(self.io.claude_working(), ['transcript:2f6571ed'])

    def test_quiet_sessions_are_idle(self):
        self.write('proj-a/aaaaaaaa-1.jsonl', 600)
        self.assertEqual(self.io.claude_working(), [])

    def test_subagent_transcripts_count_too(self):
        self.write('proj-a/bbbbbbbb-2/subagents/agent-x.jsonl', 10)
        self.assertEqual(self.io.claude_working(), ['transcript:agent-x'])

    def test_no_claude_folder_is_idle_not_an_error(self):
        self.assertEqual(self.io.claude_working(), [])

    def test_service_reports_claude_activity_and_used_up_overrides_it(self):
        busy = Mock(return_value=[])
        profiles = Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A'))
        io = Mock(codex_working=Mock(return_value=[]), claude_working=Mock(return_value=['transcript:2f6571ed']))
        s = Service(tempfile.mkdtemp(), profiles, Mock(), Mock(), busy, watch_io=io)
        s.update_activity()
        self.assertEqual(s.status()['activity']['claude']['working'], True)
        s.claude_state.update(active='CLAUDE_A', profiles={'CLAUDE_A': {'status': 'OK', '5h': {'remaining_percent': 0},
                                                                         'weekly': {'remaining_percent': 40}}})
        s.update_activity()
        c = s.status()['activity']['claude']
        self.assertEqual((c['working'], c.get('exhausted')), (False, True))

class InterruptedThreadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.io = WatchIO(self.temp.name)
        self.dir = Path(self.temp.name) / 'sessions' / '2026' / '09' / '27'
        self.dir.mkdir(parents=True)

    def thread(self, tid, lines, parent=None, age=0):
        meta = {'type': 'session_meta', 'payload': {'id': tid, 'cwd': '/work/proj', 'parent_thread_id': parent}}
        f = self.dir / f'rollout-2026-09-27T10-00-00-{tid}.jsonl'
        f.write_text(''.join(json.dumps(x) + '\n' for x in [meta] + lines))
        t = time.time() - age
        os.utime(f, (t, t))

    ctx = {'type': 'turn_context', 'payload': {'model': 'gpt-6-sol'}}
    settings = {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_settings': {'model': 'gpt-6-astra'}}}

    @staticmethod
    def ev(kind, message=None):
        return {'type': 'event_msg', 'payload': {'type': kind, 'last_agent_message': message}}

    def test_open_turn_and_silent_end_are_captured_with_latest_model(self):
        self.thread('open', [self.ctx, self.settings, self.ev('task_started')])
        self.thread('silent', [self.settings, self.ctx, self.ev('task_started'), self.ev('task_complete')])
        got = {t['thread']: t['model'] for t in self.io.interrupted_threads()}
        self.assertEqual(got, {'open': 'gpt-6-astra', 'silent': 'gpt-6-sol'})

    def test_answered_old_silent_and_subagent_threads_are_skipped(self):
        self.thread('answered', [self.ev('task_started'), self.ev('task_complete', 'DONE')])
        self.thread('oldsilent', [self.ev('task_started'), self.ev('task_complete')], age=1200)
        self.thread('child', [self.ev('task_started')], parent='open')
        self.assertEqual(self.io.interrupted_threads(), [])

    def test_long_lived_thread_in_an_old_day_folder_is_captured(self):
        # A ChatGPT-app thread started weeks ago still appends to its first day's rollout file.
        self.thread('longlived', [self.ctx, self.ev('task_started')])
        old = Path(self.temp.name) / 'sessions' / '2026' / '09' / '16'
        old.mkdir(parents=True)
        (self.dir / 'rollout-2026-09-27T10-00-00-longlived.jsonl').rename(old / 'rollout-2026-09-16T10-00-00-longlived.jsonl')
        for day in ('28', '29'):
            (Path(self.temp.name) / 'sessions' / '2026' / '09' / day).mkdir()
        self.assertEqual([t['thread'] for t in self.io.interrupted_threads()], ['longlived'])
        self.assertEqual(self.io.codex_working(), ['turn:longlived'])


if __name__ == '__main__':
    unittest.main()
