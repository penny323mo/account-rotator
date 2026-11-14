import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Codex/agy/Claude helpers in tests)
from service import Service
from quota import QuotaError
from test_priority import grow, H


def codex(five, weekly, weekly_reset=100 * H):
    return {'status': 'OK', '5h': {'remaining_percent': five, 'reset_at': time.time() + 3 * H},
            'weekly': {'remaining_percent': weekly, 'reset_at': time.time() + weekly_reset}}


class NextPickTests(unittest.TestCase):
    """Penny picks the next account; the rotator's own order applies otherwise."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {k: {} for k in 'ABCD'}
        profiles.active.return_value = 'A'
        self.broker = Mock()
        self.broker.switch.side_effect = lambda label, **kw: {'selected': label}
        self.s = Service(self.temp.name, profiles, Mock(), self.broker, Mock(return_value=[]),
                         codex=Mock(quota=Mock(return_value={'profiles': {}, 'active': None})),
                         watch_io=Mock(codex_working=Mock(return_value=[])))
        self.s.config['participants'] = list('ABCD')
        self.s.set_enabled(True)
        self.s.active = 'A'
        # the rotator's own order picks C (earliest weekly reset)
        self.s.rows = {'A': grow('A', 1, 40), 'B': grow('B', 100, 80, weekly_reset=150 * H),
                       'C': grow('C', 100, 60, weekly_reset=60 * H), 'D': grow('D', 100, 0)}

    def pick(self, provider, label):
        return self.s.call('next.pick', {'provider': provider, 'label': label})

    def test_the_picked_account_is_switched_to_and_the_pick_is_then_cleared(self):
        self.assertEqual(self.s.recommend(time.time()), 'C')
        status = self.pick('gemini', 'B')
        self.assertEqual((status['recommended'], status['next_pick']['gemini']), ('B', 'B'))
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)
        self.s.clear_used_picks()
        self.assertIsNone(self.s.next_pick['gemini'])

    def test_a_picked_account_that_cannot_take_over_is_skipped_but_kept(self):
        self.pick('gemini', 'D')  # weekly used up
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)
        self.assertEqual(self.s.next_pick['gemini'], 'D')

    def test_the_pick_survives_a_restart_and_can_be_handed_back(self):
        self.pick('gemini', 'B')
        self.assertEqual(json.loads((Path(self.temp.name) / 'next_pick.json').read_text())['gemini'], 'B')
        again = Service(self.temp.name, self.s.profiles, Mock(), self.broker, Mock(return_value=[]),
                        codex=Mock(quota=Mock(return_value={'profiles': {}, 'active': None})),
                        watch_io=Mock(codex_working=Mock(return_value=[])))
        self.assertEqual(again.next_pick['gemini'], 'B')
        self.pick('gemini', None)
        self.assertIsNone(self.s.next_pick['gemini'])
        self.assertEqual([e['kind'] for e in self.s.events][-2:], ['next_picked', 'next_pick_cleared'])

    def test_the_live_account_or_an_unknown_one_cannot_be_picked(self):
        for label in ('A', 'Z'):
            with self.assertRaises(QuotaError):
                self.pick('gemini', label)
        with self.assertRaises(QuotaError):
            self.pick('nope', 'B')

    def test_codex_and_claude_suggestions_follow_the_pick(self):
        rows = {'CODEX_A': codex(10, 50), 'CODEX_B': codex(100, 90, 150 * H), 'CODEX_C': codex(100, 60, 60 * H)}
        self.s.codex_state.update(active='CODEX_A', profiles=rows)
        now = time.time()
        self.assertEqual(self.s.recommend_other(self.s.codex_state, now), 'CODEX_C')
        self.pick('codex', 'CODEX_B')
        self.assertEqual(self.s.recommend_other(self.s.codex_state, now), 'CODEX_B')
        self.s.claude_state.update(active='CLAUDE_A', profiles={
            'CLAUDE_A': codex(10, 50), 'CLAUDE_B': codex(100, 90, 150 * H), 'CLAUDE_C': codex(100, 60, 60 * H)})
        self.pick('claude', 'CLAUDE_B')
        self.assertEqual(self.s.recommend_other(self.s.claude_state, now), 'CLAUDE_B')
        self.s.codex_state['active'] = 'CODEX_B'
        self.s.clear_used_picks()
        self.assertEqual(self.s.next_pick, {'gemini': None, 'codex': None, 'claude': 'CLAUDE_B'})


if __name__ == '__main__':
    unittest.main()
