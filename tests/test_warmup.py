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
from broker import Broker
from codex import CodexAccounts
import json
from unittest.mock import patch
from test_priority import grow, H


def codex(remaining, reset_in, active=False):
    return {'status': 'OK', 'active': active,
            '5h': {'used_percent': 100 - remaining, 'remaining_percent': remaining, 'reset_at': time.time() + reset_in},
            'weekly': {'used_percent': 10, 'remaining_percent': 90, 'reset_at': time.time() + 100 * H}}


class WarmupTests(unittest.TestCase):
    """An idle account whose 5 h window is back to 100 % but not counting down gets one tiny prompt."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {k: {} for k in 'ABC'}
        profiles.active.return_value = 'A'
        self.broker, self.codex, self.busy = Mock(), Mock(), Mock(return_value=[])
        self.codex.quota.return_value = {'profiles': {}, 'active': None}
        self.s = Service(self.temp.name, profiles, Mock(), self.broker, self.busy, codex=self.codex,
                         watch_io=Mock(codex_working=Mock(return_value=[])))
        self.s.config['participants'] = list('ABC')
        self.s.active = 'A'
        self.s.rows = {'A': grow('A', 50, 50), 'B': grow('B', 100, 80, five_reset=5 * H),
                       'C': grow('C', 90, 80, five_reset=3 * H)}
        self.s.codex_state.update(active='CODEX_A', profiles={
            'CODEX_A': codex(90, 3 * H, active=True), 'CODEX_B': codex(100, 5 * H), 'CODEX_C': codex(40, 2 * H)})
        self.now = time.time()

    def run_at(self, later):
        self.s.warm_idle(self.now + later)

    def test_waits_a_minute_then_warms_idle_gemini_and_codex_accounts(self):
        self.run_at(0)
        self.broker.warm.assert_not_called()
        self.run_at(WARM_DELAY + 1)
        self.run_at(WARM_DELAY + 2)
        self.broker.warm.assert_called_once_with('B')
        self.codex.warm.assert_called_once_with('CODEX_B')
        kinds = [(e['kind'], e.get('selected')) for e in self.s.events]
        self.assertIn(('warmed', 'B'), kinds)
        self.assertIn(('codex_warmed', 'CODEX_B'), kinds)

    def test_counting_accounts_and_busy_live_accounts_are_left_alone(self):
        self.s.watch_io.codex_working.return_value = ['turn:1']
        self.run_at(0); self.run_at(WARM_DELAY + 1); self.run_at(WARM_DELAY + 2)
        warmed = [c.args[0] for c in self.broker.warm.call_args_list + self.codex.warm.call_args_list]
        self.assertNotIn('A', warmed); self.assertNotIn('C', warmed)
        self.assertNotIn('CODEX_A', warmed); self.assertNotIn('CODEX_C', warmed)

    def test_idle_live_account_is_warmed_too(self):
        # Only one usable Codex account left and it is the live one: its idle clock must start as well.
        self.s.codex_state['profiles']['CODEX_B']['weekly']['remaining_percent'] = 0
        self.s.codex_state['profiles']['CODEX_A'] = codex(100, 5 * H, active=True)
        self.s.rows['A'] = grow('A', 100, 50, five_reset=5 * H)
        self.s.rows['B'] = grow('B', 80, 50)
        for later in (0, WARM_DELAY + 1, WARM_DELAY + 2):
            self.run_at(later)
        self.codex.warm.assert_called_once_with('CODEX_A')
        self.broker.warm.assert_called_once_with('A')

    def test_gemini_waits_while_agy_is_working(self):
        self.busy.return_value = ['agy_print:1']
        self.run_at(0); self.run_at(WARM_DELAY + 1)
        self.broker.warm.assert_not_called()
        self.codex.warm.assert_called_once_with('CODEX_B')

    def test_busy_helper_is_retried_next_poll_without_event(self):
        self.broker.warm.side_effect = [QuotaError('WARM_BUSY'), {'status': 'PASS'}]
        self.run_at(0); self.run_at(WARM_DELAY + 1); self.run_at(WARM_DELAY + 2)
        self.assertEqual(self.broker.warm.call_count, 2)
        self.assertNotIn('warm_failed', [e['kind'] for e in self.s.events])

    def test_failure_is_recorded_and_not_retried_every_poll(self):
        self.broker.warm.side_effect = QuotaError('WARM_FAILED')
        self.run_at(0); self.run_at(WARM_DELAY + 1); self.run_at(WARM_DELAY + 2); self.run_at(WARM_DELAY + 3)
        self.assertEqual(self.broker.warm.call_count, 1)
        self.assertIn('warm_failed', [e['kind'] for e in self.s.events])

    def test_switch_off_per_provider(self):
        self.s.warmup_set({'provider': 'gemini', 'enabled': False})
        self.run_at(0); self.run_at(WARM_DELAY + 1)
        self.broker.warm.assert_not_called()
        self.codex.warm.assert_called_once_with('CODEX_B')
        self.assertEqual(self.s.status()['warmup'], {'gemini': False, 'codex': True, 'claude': True})

    def test_setting_survives_restart(self):
        self.s.warmup_set({'provider': 'codex', 'enabled': False})
        again = Service(self.temp.name, Mock(list=Mock(return_value={}), active=Mock(return_value=None)), Mock(),
                        Mock(), Mock(return_value=[]), codex=self.codex, watch_io=Mock())
        self.assertEqual(again.status()['warmup'], {'gemini': True, 'codex': False, 'claude': True})

    def test_no_warm_up_during_switch_or_enrollment(self):
        self.s.switching = True
        self.s.codex_state['switching'] = True
        self.run_at(0); self.run_at(WARM_DELAY + 1)
        self.broker.warm.assert_not_called()
        self.codex.warm.assert_not_called()


def proc(code=0, out=None, err=b''):
    return Mock(returncode=code, stdout=json.dumps(out).encode() if out is not None else b'', stderr=err)


class HelperWarmTests(unittest.TestCase):
    def gemini(self, result):
        broker = Broker('/unused', Mock(), Path('/helper/agy-account'))
        with patch('broker.subprocess.run', return_value=result) as run:
            try:
                return broker.warm('B')
            finally:
                self.assertEqual(run.call_args.args[0], ['/helper/agy-account', '--json', 'warm', 'B'])

    def codex(self, result):
        return CodexAccounts('/helper/codex-account', runner=Mock(return_value=result)).warm('CODEX_B')

    def test_pass(self):
        self.assertEqual(self.gemini(proc(0, {'status': 'PASS', 'restore_ok': True}))['status'], 'PASS')
        self.assertEqual(self.codex(proc(0, {'status': 'PASS'}))['status'], 'PASS')

    def test_busy(self):
        for call in (self.gemini, self.codex):
            with self.assertRaises(QuotaError) as e:
                call(proc(1, err=b'ERROR: BUSY agy is running (pids [1]); warm up later\n'))
            self.assertEqual(str(e.exception), 'WARM_BUSY')

    def test_account_unavailable(self):
        with self.assertRaises(QuotaError) as e:
            self.codex(proc(3, {'status': 'PROFILE_ACTIVATED_BUT_ACCOUNT_UNAVAILABLE'}))
        self.assertEqual(str(e.exception), 'WARM_ACCOUNT_UNAVAILABLE')

    def test_gemini_restore_problem_is_loud(self):
        with self.assertRaises(QuotaError) as e:
            self.gemini(proc(3, {'status': 'PASS', 'restore_ok': False}))
        self.assertEqual(str(e.exception), 'WARM_RESTORE_CHECK_CURRENT')


if __name__ == '__main__':
    unittest.main()
