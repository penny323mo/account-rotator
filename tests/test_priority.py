import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import Service
from test_codex import codex_row

H = 3600


def iso(offset):
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()


def grow(label, five, weekly, five_reset=4 * H, weekly_reset=100 * H):
    return {'label': label, 'status': 'OK', 'updated_at': iso(0),
            'groups': [{'family': 'gemini', 'windows': {
                '5h': {'remaining_percent': five, 'reset_at': iso(five_reset)},
                'weekly': {'remaining_percent': weekly, 'reset_at': iso(weekly_reset)}}}]}


class GeminiPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {k: {} for k in 'ABCDE'}
        profiles.active.return_value = 'A'
        self.broker = Mock()
        self.broker.switch.side_effect = lambda label, **kw: {'selected': label}
        self.s = Service(self.temp.name, profiles, Mock(), self.broker, Mock(return_value=[]),
                         codex=Mock(quota=Mock(return_value={'profiles': {}, 'active': None})),
                         watch_io=Mock(codex_working=Mock(return_value=[])))
        self.s.config['participants'] = list('ABCDE')
        self.s.set_enabled(True)
        self.s.active = 'A'

    def rows(self, **rows):
        self.s.rows = rows

    def test_earliest_weekly_reset_goes_first(self):
        self.rows(A=grow('A', 1, 40), B=grow('B', 100, 46, weekly_reset=112 * H),
                  C=grow('C', 100, 60, weekly_reset=60 * H), D=grow('D', 100, 75, weekly_reset=168 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)
        self.assertIn('60', self.s.events[-1].get('why', ''))

    def test_quota_about_to_expire_goes_first(self):
        # D resets in 44 h with 57 % left: unused, it is lost; C (82 %) can wait until 10/7.
        self.rows(A=grow('A', 1, 77, weekly_reset=103 * H), C=grow('C', 100, 82, weekly_reset=104 * H),
                  D=grow('D', 100, 57, weekly_reset=44 * H), E=grow('E', 100, 41, weekly_reset=45 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('D', close_all=True)

    def test_quota_expiring_tonight_is_taken_below_the_switch_away_line(self):
        # 2026-10-04 19:24: C 18.99 % weekly, reset 22:54; F 99.96 % resets in 6 days and is ticking anyway.
        self.s.config['remaining_below'] = 20
        self.rows(A=grow('A', 1, 26), C=grow('C', 93, 19, weekly_reset=3.5 * H),
                  E=grow('E', 100, 99.96, weekly_reset=145 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_live_account_expiring_tonight_is_burned_not_switched_away(self):
        self.s.config['remaining_below'] = 20
        self.rows(A=grow('A', 90, 18, weekly_reset=3 * H), E=grow('E', 100, 99.96, weekly_reset=145 * H))
        self.s.tick()
        self.broker.switch.assert_not_called()  # neither "below 20 %" nor "park below 15 %"

    def test_burned_expiring_account_moves_on_when_weekly_runs_out(self):
        self.s.config['remaining_below'] = 20
        self.rows(A=grow('A', 90, 0.5, weekly_reset=3 * H), E=grow('E', 100, 99.96, weekly_reset=145 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('E', close_all=True)

    def test_burned_expiring_account_moves_on_when_its_5h_runs_out(self):
        self.s.config['remaining_below'] = 20
        self.rows(A=grow('A', 3, 18, weekly_reset=3 * H), E=grow('E', 100, 99.96, weekly_reset=145 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('E', close_all=True)

    def test_below_20_resetting_within_6_hours_beats_quota_expiring_in_2_days(self):
        self.s.config['remaining_below'] = 20
        self.rows(A=grow('A', 1, 26), C=grow('C', 93, 12, weekly_reset=5 * H),
                  D=grow('D', 100, 57, weekly_reset=44 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_below_20_resetting_later_than_6_hours_is_not_taken(self):
        self.s.config['remaining_below'] = 20
        self.rows(A=grow('A', 1, 26), C=grow('C', 93, 19, weekly_reset=10 * H),
                  E=grow('E', 100, 99.96, weekly_reset=145 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('E', close_all=True)

    def test_live_account_moves_to_quota_lost_within_6_hours_once_idle(self):
        self.s.config['remaining_below'] = 20
        self.rows(A=grow('A', 67, 26, weekly_reset=100 * H), C=grow('C', 93, 19, weekly_reset=3.5 * H),
                  F=grow('F', 100, 99.96, weekly_reset=145 * H))
        self.s.config['participants'] = list('ACF')
        self.s.busy.return_value = ['agy_print:1']
        self.s.tick()
        self.broker.switch.assert_not_called()  # never interrupts work for it
        self.assertEqual((self.s.pending['reason'], self.s.pending['target']), ('urgent', 'C'))
        self.s.busy.return_value = []
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_both_reset_tonight_the_earlier_one_is_burned_first(self):
        # 2026-10-04 19:32: live D 25 % resets 23:14, C 19 % resets 22:54: use C first, D after.
        self.s.config['remaining_below'] = 20
        self.s.config['participants'] = list('ACF')
        self.rows(A=grow('A', 59, 25, weekly_reset=3.7 * H), C=grow('C', 93, 19, weekly_reset=3.4 * H),
                  F=grow('F', 100, 99.96, weekly_reset=145 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_burning_live_account_stays_when_the_other_resets_later(self):
        self.s.config['remaining_below'] = 20
        self.s.config['participants'] = list('ACF')
        self.rows(A=grow('A', 59, 19, weekly_reset=3.4 * H), C=grow('C', 93, 25, weekly_reset=3.7 * H),
                  F=grow('F', 100, 99.96, weekly_reset=145 * H))
        self.s.tick()
        self.broker.switch.assert_not_called()

    def test_same_weekly_band_earliest_weekly_reset_first(self):
        self.rows(A=grow('A', 1, 40), C=grow('C', 100, 62, weekly_reset=60 * H),
                  D=grow('D', 100, 65, weekly_reset=100 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_then_earliest_5h_reset_first(self):
        self.rows(A=grow('A', 1, 40), C=grow('C', 60, 62, five_reset=1 * H, weekly_reset=100 * H),
                  D=grow('D', 100, 65, five_reset=5 * H, weekly_reset=100 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_above_15_percent_is_a_normal_candidate(self):
        self.rows(A=grow('A', 1, 40), E=grow('E', 100, 16, weekly_reset=60 * H),
                  B=grow('B', 0, 46, weekly_reset=112 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('E', close_all=True)

    def test_fresh_account_is_not_preferred(self):
        # The 5 h warm-up starts a fresh account's weekly clock; no need to spend it first.
        self.rows(A=grow('A', 1, 40), C=grow('C', 100, 37.7, weekly_reset=100 * H),
                  D=grow('D', 100, 99.9, weekly_reset=144 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_untouched_account_waits_for_its_later_reset(self):
        self.rows(A=grow('A', 1, 40), B=grow('B', 100, 37, weekly_reset=100 * H),
                  D=grow('D', 100, 100, weekly_reset=144 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_small_remainder_is_kept_as_backup_even_if_it_resets_first(self):
        self.rows(A=grow('A', 1, 40), E=grow('E', 100, 12, weekly_reset=10 * H),
                  B=grow('B', 100, 46, weekly_reset=112 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_backup_is_used_when_every_other_account_hit_its_5h_cap(self):
        self.rows(A=grow('A', 1, 40), E=grow('E', 100, 12, weekly_reset=60 * H),
                  B=grow('B', 0, 46, weekly_reset=112 * H), D=grow('D', 2, 90, weekly_reset=150 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('E', close_all=True)

    def test_backup_needs_about_three_hours_of_quota(self):
        self.rows(A=grow('A', 1, 40), E=grow('E', 100, 8, weekly_reset=60 * H), B=grow('B', 0, 46))
        self.s.tick()
        self.broker.switch.assert_not_called()

    def test_live_account_below_15_is_parked_as_backup(self):
        self.rows(A=grow('A', 80, 12, weekly_reset=100 * H), B=grow('B', 100, 46, weekly_reset=112 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_parking_while_busy_counts_down_and_switches(self):
        # A never-idle worker must not keep a < 15 % account running down to 0.
        self.rows(A=grow('A', 80, 12, weekly_reset=100 * H), B=grow('B', 100, 46, weekly_reset=112 * H))
        self.s.busy.return_value = ['agy_print:1']
        now = time.time()
        self.s.tick(now)
        self.assertEqual(self.s.pending['state'], 'COUNTDOWN')
        self.broker.switch.assert_not_called()
        self.s.tick(now + self.s.config['countdown_seconds'] + 1)
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_parking_while_busy_waits_when_close_app_is_off(self):
        self.s.config['close_app'] = False
        self.rows(A=grow('A', 80, 12, weekly_reset=100 * H), B=grow('B', 100, 46, weekly_reset=112 * H))
        self.s.busy.return_value = ['agy_print:1']
        self.s.tick(time.time())
        self.assertEqual(self.s.pending['state'], 'WAITING_FOR_IDLE')
        self.broker.switch.assert_not_called()

    def test_backup_in_use_keeps_running_while_others_are_capped(self):
        self.rows(A=grow('A', 80, 12, weekly_reset=100 * H), B=grow('B', 0, 46, weekly_reset=112 * H))
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertIsNone(self.s.pending)

    def test_parking_without_another_account_just_keeps_running(self):
        self.rows(A=grow('A', 80, 12, weekly_reset=100 * H), B=grow('B', 100, 3, weekly_reset=112 * H))
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertIsNone(self.s.pending)

    def test_too_little_usable_is_skipped(self):
        self.rows(A=grow('A', 1, 40), E=grow('E', 100, 4, weekly_reset=10 * H),
                  B=grow('B', 100, 46, weekly_reset=112 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_earlier_reset_wins_even_with_less_left(self):
        self.rows(A=grow('A', 1, 40), B=grow('B', 100, 30, weekly_reset=60 * H),
                  C=grow('C', 100, 70, weekly_reset=63 * H))
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_stay_when_live_account_resets_within_15_minutes(self):
        self.rows(A=grow('A', 1, 40, five_reset=10 * 60), B=grow('B', 100, 46))
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertEqual(self.s.pending['state'], 'WAITING_FOR_RESET')

    def test_recommendation_uses_same_order(self):
        self.rows(A=grow('A', 50, 40), B=grow('B', 100, 46, weekly_reset=112 * H),
                  C=grow('C', 100, 60, weekly_reset=60 * H))
        self.assertEqual(self.s.status()['recommended'], 'C')


class CodexPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rows = {}
        self.codex = Mock()
        self.codex.quota.side_effect = lambda: {'profiles': json.loads(json.dumps(self.rows)),
                                                'active': next(k for k, r in self.rows.items() if r['active'])}
        self.codex.use.side_effect = lambda label: {'selected': label, 'previous': None, 'codex_running': [], 'app_reopened': True}
        self.s = Service(self.temp.name, Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A')), Mock(), Mock(),
                         Mock(return_value=[]), codex=self.codex, watch_io=Mock(codex_working=Mock(return_value=[])))
        self.s.call('codex.auto.set', {'enabled': True})

    def crow(self, five, weekly, active=False, five_reset=4 * H, weekly_reset=100 * H):
        r = codex_row(five=five, weekly=weekly, active=active)
        t0 = self.__dict__.setdefault('t0', time.time())  # one clock for every row: equal offsets tie exactly
        r['5h']['reset_at'] = t0 + five_reset
        r['weekly']['reset_at'] = t0 + weekly_reset
        return r

    def test_codex_prefers_earliest_weekly_reset(self):
        self.rows = {'CODEX_A': self.crow(0, 50, active=True), 'CODEX_B': self.crow(90, 90, weekly_reset=150 * H),
                     'CODEX_C': self.crow(40, 40, weekly_reset=30 * H)}
        self.s.refresh_codex()
        self.codex.use.assert_called_once_with('CODEX_C')

    def pick(self, **others):
        self.s.config['remaining_below'] = 20  # a typical setting
        self.codex.use.reset_mock()
        self.rows = {'CODEX_A': self.crow(0, 50, active=True), **others}
        self.s.last_codex_switch = 0
        self.s.refresh_codex()
        return self.codex.use.call_args[0][0] if self.codex.use.called else None

    def test_codex_quota_lost_within_6_hours_goes_first_even_below_20_percent(self):
        # Under 20 % but resetting within 6 h is absolute priority (same as Antigravity).
        self.assertEqual(self.pick(CODEX_B=self.crow(100, 99.96, weekly_reset=145 * H),
                                   CODEX_C=self.crow(90, 12, weekly_reset=5 * H)), 'CODEX_C')

    def test_codex_urgent_ones_are_ordered_by_earliest_reset(self):
        self.assertEqual(self.pick(CODEX_B=self.crow(90, 30, weekly_reset=5 * H),
                                   CODEX_C=self.crow(90, 40, weekly_reset=2 * H)), 'CODEX_C')

    def test_codex_account_with_a_nearly_empty_5h_window_is_a_last_resort_even_if_it_expires_soon(self):
        self.assertEqual(self.pick(CODEX_B=self.crow(100, 60, weekly_reset=100 * H),
                                   CODEX_C=self.crow(5, 30, weekly_reset=3 * H)), 'CODEX_B')

    def test_codex_expiring_within_48_hours_beats_a_later_reset(self):
        self.assertEqual(self.pick(CODEX_B=self.crow(100, 80, weekly_reset=100 * H),
                                   CODEX_C=self.crow(100, 40, weekly_reset=44 * H)), 'CODEX_C')

    def test_codex_fresh_account_is_not_preferred_earliest_reset_wins(self):
        self.assertEqual(self.pick(CODEX_B=self.crow(100, 99.9, weekly_reset=144 * H),
                                   CODEX_C=self.crow(100, 38, weekly_reset=100 * H)), 'CODEX_C')

    def test_codex_then_earliest_5h_reset(self):
        self.assertEqual(self.pick(CODEX_B=self.crow(100, 62, five_reset=5 * H),
                                   CODEX_C=self.crow(60, 62, five_reset=1 * H)), 'CODEX_C')

    def test_codex_account_under_15_percent_weekly_is_the_last_resort(self):
        self.assertEqual(self.pick(CODEX_B=self.crow(100, 10, weekly_reset=60 * H),
                                   CODEX_C=self.crow(100, 70, weekly_reset=140 * H)), 'CODEX_C')
        self.assertEqual(self.pick(CODEX_B=self.crow(100, 10, weekly_reset=60 * H)), 'CODEX_B')

    def test_status_names_the_suggested_codex_candidate_with_the_same_order(self):
        self.rows = {'CODEX_A': self.crow(60, 50, active=True), 'CODEX_B': self.crow(100, 80, weekly_reset=100 * H),
                     'CODEX_C': self.crow(100, 40, weekly_reset=44 * H)}
        self.s.refresh_codex()
        self.assertEqual(self.s.status()['codex']['recommended'], 'CODEX_C')

    def test_no_suggestion_when_the_only_other_account_is_used_up_or_missing(self):
        self.rows = {'CODEX_A': self.crow(60, 50, active=True)}
        self.s.refresh_codex()
        self.assertIsNone(self.s.status()['codex']['recommended'])
        self.rows['CODEX_B'] = self.crow(0, 50)
        self.s.refresh_codex()
        self.assertIsNone(self.s.status()['codex']['recommended'])

    def test_codex_waits_when_live_resets_soon(self):
        self.rows = {'CODEX_A': self.crow(0, 50, active=True, five_reset=5 * 60), 'CODEX_B': self.crow(90, 90)}
        self.s.refresh_codex()
        self.codex.use.assert_not_called()


if __name__ == '__main__':
    unittest.main()
