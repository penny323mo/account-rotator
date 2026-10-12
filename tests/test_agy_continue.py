import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real agy terminal is read or typed into)
from agy_continue import AgyContinue
from quota import QuotaError
from service import Service

STOPPED = """> fix the parser
  ● Reading src/parser.py
  ✕ You have exhausted your quota on this model.
> """
WORKING = """> fix the parser
  ● Reading src/parser.py
  ⠋ Thinking…"""


class FakeIO:
    def __init__(self):
        self.screens = {('tmux', '%5'): STOPPED, ('tmux', '%6'): WORKING}
        self.typed = []

    def panes(self):
        return [('%5', '/w/slot', 100), ('%6', '/w/room', 0)]

    def screen(self, kind, target):
        return self.screens[(kind, target)]

    def type(self, kind, target, text):
        self.typed.append((kind, target, text))


class AgyContinueTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.a = AgyContinue(self.io)

    def test_only_the_session_showing_the_limit_is_stopped(self):
        self.assertEqual(self.a.stopped(), [{'kind': 'tmux', 'target': '%5', 'cwd': '/w/slot', 'at': 100}])

    def test_text_is_typed_into_its_own_terminal(self):
        self.assertIsNone(self.a.send(self.a.stopped()[0], '繼續'))
        self.assertEqual(self.io.typed, [('tmux', '%5', '繼續')])

    def test_nothing_is_typed_once_it_moved_on(self):
        session = self.a.stopped()[0]
        self.io.screens[('tmux', '%5')] = WORKING
        self.assertEqual(self.a.send(session), 'NOT_STOPPED')
        self.assertEqual(self.io.typed, [])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agy = Mock(stopped=Mock(return_value=[{'kind': 'tmux', 'target': '%5', 'cwd': '/w/slot'}]),
                        send=Mock(return_value=None))
        self.s = Service(self.temp.name, Mock(list=Mock(return_value={}), active=Mock(return_value=None)), Mock(),
                         Mock(), Mock(return_value=[]), agy_continue=self.agy)
        self.s.active = 'GEMINI_A'
        self.s.agy_refresh_stopped()

    def wait(self):
        for _ in range(200):
            if not self.s.agy_continuing:
                return
            time.sleep(0.01)

    def test_listed_and_continued_with_the_users_text(self):
        self.assertEqual([t['project'] for t in self.s.status()['agy_stopped']], ['slot'])
        self.s.call('agy.continue.now', {'text': 'continue please'})
        self.wait()
        self.agy.send.assert_called_once()
        self.assertEqual(self.agy.send.call_args[0][1], 'continue please')
        self.assertEqual(self.s.events[-1]['kind'], 'agy_manualcontinue')

    def test_keeps_talking_to_the_session_it_continued(self):
        self.s.call('agy.continue.now', {})
        self.wait()
        self.agy.stopped.return_value = [{'kind': 'tmux', 'target': '%9', 'cwd': '/w/other'}]
        self.s.agy_refresh_stopped()
        self.s.call('agy.continue.now', {'text': 'go on'})
        self.wait()
        self.assertEqual(self.agy.send.call_args[0][0]['target'], '%5')
        self.assertEqual(self.agy.send.call_args[1], {'check': False})

    def test_preview_is_the_screen_bottom(self):
        self.agy.last_output = Mock(return_value='> fix\n✕ You have exhausted your quota on this model.')
        r = self.s.call('continue.preview', {'provider': 'agy'})
        self.assertEqual((r['project'], r['target']), ('slot', False))
        self.assertIn('exhausted', r['text'])
        self.agy.send.assert_not_called()

    def test_refused_while_the_live_account_is_used_up(self):
        self.s.rows['GEMINI_A'] = {'status': 'OK', 'groups': [{'family': 'gemini', 'windows': {
            '5h': {'remaining_percent': 0, 'reset_at': None}, 'weekly': {'remaining_percent': 50, 'reset_at': None}}}]}
        with self.assertRaises(QuotaError) as e:
            self.s.call('agy.continue.now', {})
        self.assertEqual(str(e.exception), 'ACTIVE_EXHAUSTED')
        self.agy.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
