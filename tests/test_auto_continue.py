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
from test_codex import codex_row

T1 = '01a0aa3c-1e93-79e1-89ea-c83e3001e106'


class AutoContinueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rows = {'CODEX_A': codex_row(five=0, active=True), 'CODEX_B': codex_row(five=60, weekly=40)}
        self.codex = Mock()
        self.codex.quota.side_effect = lambda: {'profiles': json.loads(json.dumps(self.rows)),
                                                'active': next(k for k, r in self.rows.items() if r['active'])}
        self.codex.use.side_effect = lambda label: {'selected': label, 'previous': None, 'codex_running': [], 'app_reopened': True}
        self.codex.continue_thread.return_value = {'tmux': 'codex-continue:codex-01e106', 'watched': True}
        self.io = Mock()
        self.io.interrupted_threads.return_value = [{'thread': T1, 'model': 'gpt-6-astra', 'cwd': '/work/my-project'}]
        self.io.codex_working.return_value = []
        profiles = Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A'))
        client = Mock(fetch=Mock(side_effect=lambda label: row(label)))
        self.s = Service(self.temp.name, profiles, client, Mock(), Mock(return_value=[]), codex=self.codex, watch_io=self.io)
        self.s.call('codex.auto.set', {'enabled': True})

    def test_off_by_default_switches_without_continuing(self):
        self.s.refresh_codex()
        self.codex.use.assert_called_once_with('CODEX_B')
        self.codex.continue_thread.assert_not_called()

    def test_on_resumes_threads_captured_before_switch_with_their_model(self):
        self.s.call('codex.autocontinue.set', {'enabled': True})
        self.s.refresh_codex()
        self.io.interrupted_threads.assert_called_once()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')
        kinds = [e['kind'] for e in self.s.events]
        self.assertLess(kinds.index('codex_switched'), kinds.index('codex_autocontinue'))

    def test_failed_switch_does_not_continue(self):
        self.s.call('codex.autocontinue.set', {'enabled': True})
        self.codex.use.side_effect = QuotaError('CLOSE_TIMEOUT_NOT_SWITCHED')
        self.s.refresh_codex()
        self.codex.continue_thread.assert_not_called()

    def test_continue_failure_is_recorded_not_raised(self):
        self.s.call('codex.autocontinue.set', {'enabled': True})
        self.codex.continue_thread.side_effect = QuotaError('CODEX_CONTINUE_FAILED')
        self.s.refresh_codex()
        self.assertEqual(self.s.events[-1]['kind'], 'codex_autocontinue_failed')

    def test_manual_switch_never_auto_continues(self):
        self.s.call('codex.autocontinue.set', {'enabled': True})
        self.rows['CODEX_A'] = codex_row(five=50, active=True)
        self.s.refresh_codex()
        self.s.call('codex.use', {'label': 'CODEX_B'})
        self.codex.continue_thread.assert_not_called()

    def test_setting_persists(self):
        self.s.call('codex.autocontinue.set', {'enabled': True})
        again = Service(self.temp.name, Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A')),
                        Mock(fetch=Mock(side_effect=lambda label: row(label))), Mock(), Mock(return_value=[]),
                        codex=self.codex, watch_io=self.io)
        self.assertTrue(again.status()['codex']['autocontinue'])
        with self.assertRaises(QuotaError):
            self.s.call('codex.autocontinue.set', {'enabled': 'yes'})


if __name__ == '__main__':
    unittest.main()
