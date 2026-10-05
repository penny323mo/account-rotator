import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import Service, WARM_DELAY
from quota import QuotaError
from test_priority import grow, H
from test_warmup import codex


class ExhaustedAccountTests(unittest.TestCase):
    """An account with 0 % left (5 h or weekly) is never a switch target: automatic, manual or queued."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {k: {} for k in 'ABC'}
        profiles.active.return_value = 'C'
        self.broker = Mock()
        self.broker.switch.side_effect = lambda label, **kw: {'selected': label}
        self.codex = Mock()
        self.codex.quota.return_value = {'profiles': {}, 'active': None}
        self.codex.use.side_effect = lambda label: {'selected': label}
        self.s = Service(self.temp.name, profiles, Mock(), self.broker, Mock(return_value=[]), codex=self.codex,
                         watch_io=Mock(codex_working=Mock(return_value=[]), interrupted_threads=Mock(return_value=[])))
        self.s.config['participants'] = list('ABC')
        self.s.set_enabled(True)
        self.s.active = 'C'

    def test_used_up_live_account_switches_with_force(self):
        self.s.rows = {'C': grow('C', 0, 40), 'B': grow('B', 100, 46, weekly_reset=100 * H)}
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True, force=True)

    def test_low_but_not_used_up_account_does_not_force(self):
        self.s.rows = {'C': grow('C', 1, 40), 'B': grow('B', 100, 46, weekly_reset=100 * H)}
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_auto_skips_weekly_empty_account_even_with_earliest_reset(self):
        self.s.rows = {'C': grow('C', 1, 40), 'A': grow('A', 36, 0, weekly_reset=10 * H),
                       'B': grow('B', 100, 46, weekly_reset=100 * H)}
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_auto_does_not_switch_when_only_empty_accounts_remain(self):
        self.s.rows = {'C': grow('C', 1, 40), 'A': grow('A', 36, 0), 'B': grow('B', 0, 80)}
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertEqual(self.s.recommend(time.time()), None)

    def test_manual_switch_to_empty_account_is_allowed(self):
        # The user's explicit choice wins; only automatic rotation skips used-up accounts.
        self.s.rows = {'C': grow('C', 50, 50), 'A': grow('A', 36, 0), 'B': grow('B', 100, 46)}
        self.s.manual_use({'label': 'A', 'now': True})
        self.broker.switch.assert_called_once_with('A', close_all=True)

    def test_queued_manual_switch_still_runs_if_target_ran_out(self):
        self.s.rows = {'C': grow('C', 50, 50), 'A': grow('A', 36, 5), 'B': grow('B', 100, 46)}
        self.s.manual_use({'label': 'A', 'wait_idle': True})
        self.s.rows['A'] = grow('A', 36, 0)
        self.s.tick()
        self.broker.switch.assert_called_once_with('A', close_all=True)

    def test_codex_auto_skips_empty_account_but_manual_can_choose_it(self):
        self.s.codex_state.update(auto=True, active='CODEX_A', profiles={
            'CODEX_A': codex(0, 2 * H, active=True), 'CODEX_B': {**codex(100, 5 * H), 'weekly': {
                'used_percent': 100, 'remaining_percent': 0, 'reset_at': time.time() + 10 * H}}})
        self.s.codex_auto(time.time())
        self.codex.use.assert_not_called()
        self.s.codex_use({'label': 'CODEX_B'})  # manual: allowed
        self.codex.use.assert_called_once_with('CODEX_B')

    def test_no_warm_up_for_weekly_empty_account(self):
        self.s.rows = {'C': grow('C', 50, 50), 'A': grow('A', 100, 0, five_reset=5 * H),
                       'B': grow('B', 90, 46)}
        now = time.time()
        self.s.warm_idle(now); self.s.warm_idle(now + WARM_DELAY + 1)
        self.broker.warm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
