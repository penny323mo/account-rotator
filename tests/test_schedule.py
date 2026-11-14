import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Codex/agy/Claude helpers in tests)
from service import Service, SCHEDULE_GRACE
from quota import QuotaError


class ScheduleTests(unittest.TestCase):
    """2026-11-14: Penny wants CODEX_C kept for later - 23:30 to CODEX_B, 01:30 to CODEX_A, queued in advance."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {k: {} for k in ('GEMINI_A', 'GEMINI_B')}
        profiles.active.return_value = 'GEMINI_A'
        self.broker = Mock()
        self.codex = Mock(app_running=Mock(return_value=True))
        self.codex.use.side_effect = lambda label: {'selected': label}
        self.claude = Mock()
        self.claude.use.side_effect = lambda label: {'selected': label}
        self.watch = Mock(codex_working=Mock(return_value=[]),
                          interrupted_threads=Mock(return_value=[{'thread': 't1', 'cwd': '/w/room', 'model': 'm'}]))
        self.s = Service(self.temp.name, profiles, Mock(), self.broker, Mock(return_value=[]), codex=self.codex,
                         claude=self.claude, watch_io=self.watch)
        self.s.config['participants'] = ['GEMINI_A', 'GEMINI_B']
        self.s.active = 'GEMINI_A'
        self.s.codex_state.update(active='CODEX_C', autocontinue=True,
                                  profiles={k: {'status': 'OK'} for k in ('CODEX_A', 'CODEX_B', 'CODEX_C')})
        self.s.claude_state.update(active='CLAUDE_A', profiles={k: {'status': 'OK'} for k in ('CLAUDE_A', 'CLAUDE_B')})
        self.now = time.time()

    def add(self, provider, label, later):
        return self.s.call('schedule.add', {'provider': provider, 'label': label, 'at': self.now + later})

    def test_queued_switches_run_in_order_each_at_its_time(self):
        self.add('codex', 'CODEX_A', 2 * 3600)
        status = self.add('codex', 'CODEX_B', 600)
        self.assertEqual([x['label'] for x in status['schedules']], ['CODEX_B', 'CODEX_A'])
        self.s.run_schedules(self.now + 60)
        self.codex.use.assert_not_called()
        self.s.run_schedules(self.now + 601)
        self.codex.use.assert_called_once_with('CODEX_B')
        self.codex.continue_thread.assert_called_once_with('t1', 'm')  # like an automatic switch
        self.assertEqual(self.s.codex_state['active'], 'CODEX_B')
        self.s.run_schedules(self.now + 2 * 3600 + 1)
        self.assertEqual(self.s.codex_state['active'], 'CODEX_A')
        self.assertEqual(self.s.schedules, [])
        kinds = [e['kind'] for e in self.s.events]
        self.assertEqual(kinds.count('codex_schedule_done'), 2)
        self.assertIn('scheduled', [e.get('source') for e in self.s.events if e['kind'] == 'codex_switched'])

    def test_a_busy_switch_is_tried_again_and_other_errors_are_reported(self):
        self.add('codex', 'CODEX_B', 60)
        self.codex.use.side_effect = QuotaError('SWITCH_BUSY')
        with patch('service.time.sleep'):
            self.s.run_schedules(self.now + 61)
        self.assertEqual(len(self.s.schedules), 1)
        self.codex.use.side_effect = QuotaError('CODEX_SWITCH_FAILED')
        with patch('service.time.sleep'):
            self.s.run_schedules(self.now + 120)
        self.assertEqual(self.s.schedules, [])
        failed = [e for e in self.s.events if e['kind'] == 'codex_schedule_failed']
        self.assertEqual(failed[-1]['error'], 'CODEX_SWITCH_FAILED')

    def test_a_busy_switch_gives_up_after_the_grace_period(self):
        self.add('codex', 'CODEX_B', 60)
        self.codex.use.side_effect = QuotaError('SWITCH_BUSY')
        with patch('service.time.sleep'):
            self.s.run_schedules(self.now + 60 + SCHEDULE_GRACE + 1)
        self.assertEqual(self.s.schedules, [])

    def test_antigravity_waits_for_idle_and_claude_switches(self):
        self.add('gemini', 'GEMINI_B', 60)
        self.add('claude', 'CLAUDE_B', 60)
        self.s.run_schedules(self.now + 61)
        self.assertEqual((self.s.pending or {}).get('state'), 'WAITING_FOR_IDLE')
        self.assertEqual(self.s.pending['target'], 'GEMINI_B')
        self.claude.use.assert_called_once_with('CLAUDE_B')

    def test_already_on_that_account_counts_as_done(self):
        self.add('codex', 'CODEX_C', 60)
        self.s.run_schedules(self.now + 61)
        self.codex.use.assert_not_called()
        self.assertEqual(self.s.events[-1]['kind'], 'codex_schedule_done')

    def test_removed_bad_and_restart(self):
        status = self.add('codex', 'CODEX_A', 600)
        self.assertEqual(json.loads((Path(self.temp.name) / 'schedules.json').read_text())[0]['label'], 'CODEX_A')
        again = Service(self.temp.name, self.s.profiles, Mock(), self.broker, Mock(return_value=[]), codex=self.codex,
                        claude=self.claude, watch_io=self.watch)
        self.assertEqual(len(again.schedules), 1)
        self.s.call('schedule.remove', {'id': status['schedules'][0]['id']})
        self.assertEqual(self.s.schedules, [])
        for params in ({'provider': 'codex', 'label': 'CODEX_Z', 'at': self.now + 60},
                       {'provider': 'codex', 'label': 'CODEX_A', 'at': self.now - 3600},
                       {'provider': 'codex', 'label': 'CODEX_A', 'at': 'soon'}):
            with self.assertRaises(QuotaError):
                self.s.call('schedule.add', params)

    def test_the_poll_loop_wakes_for_the_next_one(self):
        self.add('codex', 'CODEX_A', 25)
        self.assertAlmostEqual(self.s.schedule_due_in(self.now), 26, delta=1)


if __name__ == '__main__':
    unittest.main()
