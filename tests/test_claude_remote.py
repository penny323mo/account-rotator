import subprocess
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Claude Code session is typed into)
from claude_remote import Relinker

PROMPT = """✻ done
────────
❯
────────
  ⏵⏵ bypass permissions on"""


class Session:
    """A Claude Code prompt: /remote-control opens a menu while a link is held, connects otherwise."""
    def __init__(self, pid, link, connected=True):
        self.pid, self.link, self.connected = pid, link, connected
        self.menu, self.selected, self.history, self.n, self.buffer, self.title = False, 2, [], 0, '', None

    def screen(self):
        if not self.menu:
            return PROMPT
        rows = ['Disconnect this session', 'Show QR code', 'Continue']
        return 'Remote Control\n' + '\n'.join(('  ❯ ' if i == self.selected else '    ') + r for i, r in enumerate(rows)) \
            + '\n  Enter to select · Esc to continue'

    def type(self, text):
        command, _, title = text.partition(' ')
        if command != '/remote-control':
            self.history.append(text)
        elif self.connected:
            self.menu, self.selected = True, 2
        else:
            self.n += 1
            self.connected, self.link, self.title = True, f'new-{self.pid}-{self.n}', title or None

    def key(self, key):
        if not self.menu and key == 'enter' and self.buffer:  # tmux: typed text runs on Enter
            text, self.buffer = self.buffer, ''
            return self.type(text)
        if self.menu and key == 'up':
            self.selected = max(0, self.selected - 1)
        elif self.menu and key == 'esc':
            self.menu = False
        elif self.menu and key == 'enter':
            self.menu = False
            if self.selected == 0:
                self.connected, self.link = False, None
        elif not self.menu and key == 'up':
            self.history.append('RECALLED LAST MESSAGE')  # what must never happen


class FakeIO:
    def __init__(self):
        self.sessions = {10: Session(10, 'old-10'), 20: Session(20, 'old-20')}
        self.meta = {10: {'name': 'Clawbook', 'cwd': '/w/clawbook', 'tmux': 'claude:@0.%0'},
                     20: {'name': 'Control room', 'cwd': '/w/room', 'tmux': 'claude:@1.%1'},
                     30: {'name': 'No remote', 'cwd': '/w/x'}}
        self.status = {10: 'idle', 20: 'idle', 30: 'idle'}
        self.panes = {'%0': 10, '%1': 20}
        self.clock = 0

    def records(self):
        out = []
        for pid, m in self.meta.items():
            s = self.sessions.get(pid)
            out.append({'pid': pid, 'sessionId': f'sess-{pid}', 'status': self.status[pid],
                        'bridgeSessionId': s.link if s else None, **m})
        return out

    def alive(self, pid):
        return pid in self.meta

    def tmux(self, pane, *keys, literal=False):
        s = self.sessions[self.panes[pane]]
        for k in keys:
            if literal:
                s.buffer += k
            else:
                s.key({'Up': 'up', 'Enter': 'enter', 'Escape': 'esc'}[k])

    def screen(self, pane):
        return self.sessions[self.panes[pane]].screen()

    def typed_screen(self, pane):
        return self.sessions[self.panes[pane]].screen()

    stale = ()
    limits = {}

    def last_message(self, session_id):
        return self.limits.get(session_id, ('u-1', False))

    def owned(self, link):
        return not any(link.startswith(f'new-{pid}-') for pid in self.stale)

    def sleep(self, seconds):
        self.clock += seconds

    def now(self):
        return self.clock

    never = ()    # pids that never notice
    lines = {}    # pid -> Remote Control lines of a session without a link
    pokes = 0

    def notices(self, session_id):
        pid = int(session_id.split('-')[1])
        if pid not in self.sessions or self.sessions[pid].link is None:
            return self.lines.get(pid, [])
        if pid in self.never:
            return []
        return [(self.clock, NOTICE)]

    def poke(self):
        self.pokes += 1

    def each(self, jobs):  # one after another: the fake clock is shared
        for job in jobs:
            job()


NOTICE = 'Remote Control disconnected \u2014 signed-in claude.ai account or organization changed on this machine'


class LastOutputTests(unittest.TestCase):
    def test_whole_last_round_skipping_the_limit_error_and_tool_calls(self):
        import json, tempfile, claude_remote
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'proj').mkdir()
            rows = [
                {'type': 'assistant', 'timestamp': 't1', 'message': {'content': [{'type': 'text', 'text': '做完：3 個 test 過咗'}]}},
                {'type': 'assistant', 'timestamp': 't2', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash'}]}},
                {'type': 'assistant', 'timestamp': 't3', 'isApiErrorMessage': True, 'error': 'rate_limit',
                 'message': {'content': [{'type': 'text', 'text': "You've hit your session limit"}]}}]
            rows = [{'type': 'user', 'message': {'content': 'fix it'}},
                    {'type': 'assistant', 'timestamp': 't0', 'message': {'content': [{'type': 'text', 'text': '上一輪'}]}},
                    {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'run the tests'}]}},
                    {'type': 'assistant', 'timestamp': 't0b', 'message': {'content': [{'type': 'text', 'text': '開始跑'}]}},
                    {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}},
                    {'type': 'user', 'isMeta': True, 'message': {'content': 'caveat'}}] + rows
            (Path(d) / 'proj' / 'sid.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            old, claude_remote.PROJECTS = claude_remote.PROJECTS, d
            try:
                self.assertEqual(isolation.REAL_REMOTE_IO().last_output('sid'), ('開始跑\n\n做完：3 個 test 過咗', 't1'))
            finally:
                claude_remote.PROJECTS = old


class TypeLiteralTests(unittest.TestCase):
    def test_long_text_is_typed_in_pieces_tmux_accepts(self):
        from unittest import mock
        import claude_remote
        text = '繼' * 5000 + 'end'
        with mock.patch.object(claude_remote.subprocess, 'run') as run:
            claude_remote.type_literal('%1', text)
        pieces = [c.args[0][-1] for c in run.call_args_list]
        self.assertEqual(''.join(pieces), text)
        self.assertTrue(all(len(p.encode()) <= 8000 for p in pieces))  # tmux refuses ~16 KB
        self.assertTrue(all(c.args[0][1:5] == ['send-keys', '-t', '%1', '-l'] for c in run.call_args_list))


class LastErrorTests(unittest.TestCase):
    def read(self, rows):
        import json, tempfile, claude_remote
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'proj').mkdir()
            (Path(d) / 'proj' / 'sid.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            old, claude_remote.PROJECTS = claude_remote.PROJECTS, d
            try:
                return isolation.REAL_REMOTE_IO().last_error('sid')
            finally:
                claude_remote.PROJECTS = old

    def error(self, kind, text):
        return {'type': 'assistant', 'uuid': 'e1', 'timestamp': 't', 'isApiErrorMessage': True, 'error': kind,
                'message': {'content': [{'type': 'text', 'text': text}]}}

    def test_overloaded_is_an_error_stop(self):
        self.assertEqual(self.read([self.error('server_error', 'API Error: 529 Overloaded.')]),
                         {'uuid': 'e1', 'error': 'server_error', 'text': 'API Error: 529 Overloaded.', 'timestamp': 't'})

    def test_the_usage_limit_and_a_later_prompt_are_not(self):
        self.assertIsNone(self.read([self.error('rate_limit', "You've hit your session limit")]))
        self.assertIsNone(self.read([self.error('server_error', 'x'), {'type': 'user', 'message': {'content': 'retry'}}]))


class RelinkerTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.reports = []
        self.r = Relinker(self.io, pickup=45, limit=100, poll=10)

    def run_it(self):
        self.r.run(lambda rec, link, err: self.reports.append((rec['name'], link, err)))

    def test_old_link_is_disconnected_and_registered_again_under_its_name(self):
        self.run_it()
        self.assertEqual(sorted(self.reports), [('Clawbook', 'new-10-1', None), ('Control room', 'new-20-1', None)])
        for s in self.io.sessions.values():
            self.assertFalse(s.menu)          # nothing left open
            self.assertEqual(s.history, [])   # no message recalled or sent
        self.assertEqual([s.title for s in self.io.sessions.values()], ['Clawbook', 'Control room'])  # names kept

    def test_a_newer_run_stops_this_one_before_any_key(self):
        self.r.run(lambda rec, link, err: self.reports.append((rec['name'], link, err)), stop=lambda: True)
        self.assertEqual(self.reports, [])
        self.assertEqual([s.link for s in self.io.sessions.values()], ['old-10', 'old-20'])

    def test_session_without_a_held_link_just_connects(self):
        self.io.sessions[10].connected = False
        self.run_it()
        self.assertIn(('Clawbook', 'new-10-1', None), self.reports)
        self.assertEqual(self.io.sessions[10].title, 'Clawbook')  # named on the first /remote-control too

    def test_no_keys_when_the_menu_is_not_what_was_expected(self):
        orig = self.io.sessions[20].screen
        self.io.sessions[20].screen = lambda: orig().replace('Disconnect this session', 'Something else') \
            if self.io.sessions[20].selected == 0 else orig()
        self.run_it()
        self.assertIn(('Control room', None, 'MENU_UNEXPECTED'), self.reports)
        s = self.io.sessions[20]
        self.assertEqual((s.menu, s.link, s.history), (False, 'old-20', []))  # closed, untouched

    def test_waits_for_the_new_login_before_typing(self):
        def stop(seconds):
            if seconds == 45:
                raise RuntimeError('stop')
        self.io.sleep = stop
        with self.assertRaises(RuntimeError):
            self.run_it()
        self.assertEqual([s.link for s in self.io.sessions.values()], ['old-10', 'old-20'])

    def test_busy_tmux_session_is_relinked_without_waiting(self):
        self.io.status[10] = 'busy'  # /remote-control runs mid-turn
        self.run_it()
        self.assertIn(('Clawbook', 'new-10-1', None), self.reports)

    def test_session_outside_tmux_is_reported_not_guessed(self):
        del self.io.meta[20]['tmux']
        self.run_it()
        self.assertIn(('Control room', None, 'NO_TERMINAL'), self.reports)

    def test_sessions_noted_before_the_switch_are_the_ones_handled(self):
        before = {20: dict(self.io.records()[1])}
        self.r.run(lambda rec, link, err: self.reports.append((rec['name'], link, err)), before)
        self.assertEqual([n for n, _, _ in self.reports], ['Control room'])

    def test_session_still_on_the_old_login_is_reported_not_counted(self):
        self.io.stale = (20,)  # it never noticed the switch: its new link belongs to the old account
        self.run_it()
        self.assertIn(('Control room', None, 'OLD_LOGIN'), self.reports)
        self.assertIn(('Clawbook', 'new-10-1', None), self.reports)

    def test_session_stopped_on_the_limit_is_found_and_continued_at_an_empty_prompt(self):
        self.io.limits = {'sess-10': ('err-1', True)}
        stopped = self.r.stopped_sessions()
        self.assertEqual({pid: uuid for pid, (_, uuid) in stopped.items()}, {10: 'err-1'})
        self.assertIsNone(self.r.resume(stopped[10][0]))
        self.assertEqual(self.io.sessions[10].history, ['continue'])

    def test_own_text_is_typed_instead_of_continue(self):
        self.io.limits = {'sess-10': ('err-1', True)}
        self.assertIsNone(self.r.resume(self.r.stopped_sessions()[10][0], '繼續'))
        self.assertEqual(self.io.sessions[10].history, ['繼續'])

    def test_busy_session_is_not_counted_as_stopped(self):
        self.io.limits = {'sess-10': ('err-1', True)}
        self.io.status[10] = 'busy'
        self.assertEqual(self.r.stopped_sessions(), {})

    def test_nothing_is_typed_over_a_draft_or_a_menu(self):
        self.io.sessions[20].menu = True
        record = next(r for r in self.io.records() if r['pid'] == 20)
        self.assertEqual(self.r.resume(record), 'PROMPT_NOT_READY')
        self.assertEqual(self.io.sessions[20].history, [])

    def test_the_credentials_file_is_touched_before_anything_is_typed(self):
        # 2026-10-15 21:50: sessions kept their in-memory token and registered under the old account for 6 minutes
        typed = []
        self.io.poke = lambda: typed.append(('poke', list(self.io.sessions[10].history), self.io.sessions[10].link))
        self.run_it()
        self.assertEqual(typed, [('poke', [], 'old-10')])

    def test_a_session_that_never_noticed_the_switch_is_relinked_at_once(self):
        self.io.never = (20,)  # no account-changed notice, still holding the old link
        self.r.run(lambda rec, link, err: self.reports.append((rec['name'], link, err, self.io.clock)))
        room = next(r for r in self.reports if r[0] == 'Control room')
        self.assertEqual(room[1:3], ('new-20-1', None))
        self.assertLess(room[3], 100)

    def test_a_link_an_earlier_switch_dropped_is_brought_back(self):
        self.io.sessions[10].link, self.io.sessions[10].connected = None, False  # noticed the last switch
        self.io.lines = {10: [(0, 'Remote Control connected'), (5, NOTICE)]}
        before = {20: dict(self.io.records()[1])}           # only Control room had a link at this switch
        self.r.run(lambda rec, link, err: self.reports.append((rec['name'], link, err)), before)
        self.assertIn(('Clawbook', 'new-10-1', None), self.reports)

    def test_a_link_disconnected_by_hand_stays_off(self):
        self.io.sessions[10].link, self.io.sessions[10].connected = None, False
        self.io.lines = {10: [(5, NOTICE), (9, '<local-command-stdout>Remote Control disconnected.</local-command-stdout>')]}
        self.run_it()
        self.assertNotIn('Clawbook', [n for n, _, _ in self.reports])

    def test_a_draft_is_never_sent_along_with_remote_control(self):
        self.io.sessions[20].screen = lambda: PROMPT.replace('❯', '❯ 幫我寫條訊息畀 Control-room')
        self.run_it()
        self.assertIn(('Control room', None, 'PROMPT_NOT_READY'), self.reports)
        s = self.io.sessions[20]
        self.assertEqual((s.link, s.history, s.menu), ('old-20', [], False))

    def test_session_without_remote_control_is_left_alone(self):
        self.run_it()
        self.assertNotIn('No remote', [n for n, _, _ in self.reports])


if __name__ == '__main__':
    unittest.main()
