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
            out.append({'pid': pid, 'status': self.status[pid], 'bridgeSessionId': s.link if s else None, **m})
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

    stale = ()

    def owned(self, link):
        return not any(link.startswith(f'new-{pid}-') for pid in self.stale)

    def sleep(self, seconds):
        self.clock += seconds

    def now(self):
        return self.clock


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

    def test_busy_session_waits_and_is_reported_when_it_stays_busy(self):
        self.io.status[10] = 'busy'
        self.run_it()
        self.assertIn(('Clawbook', None, 'STILL_BUSY'), self.reports)
        self.assertEqual(self.io.sessions[10].link, 'old-10')  # never typed into a busy session

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

    def test_session_without_remote_control_is_left_alone(self):
        self.run_it()
        self.assertNotIn('No remote', [n for n, _, _ in self.reports])


if __name__ == '__main__':
    unittest.main()
