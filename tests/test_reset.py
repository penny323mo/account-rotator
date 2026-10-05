import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Codex/agy/Claude helpers in tests)
from service import Service
from quota import QuotaError


class ResetTests(unittest.TestCase):
    """重設: clear stuck state, close everything of that tool and go back to its first account."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {k: {} for k in 'CAB'}
        profiles.active.return_value = 'C'
        self.broker = Mock()
        self.broker.switch.side_effect = lambda label, **kw: {'selected': label}
        self.codex = Mock(app_running=Mock(return_value=True), open_app=Mock(return_value=True))
        self.codex.quota.return_value = {'profiles': {'CODEX_B': {'active': True}, 'CODEX_A': {}}, 'active': 'CODEX_B'}
        self.codex.use.side_effect = lambda label: {'selected': label}
        self.claude = Mock()
        self.claude.quota.return_value = {'profiles': {'CLAUDE_B': {'active': True}, 'CLAUDE_A': {}}, 'active': 'CLAUDE_B'}
        self.claude.use.side_effect = lambda label: {'selected': label}
        self.s = Service(self.temp.name, profiles, Mock(), self.broker, Mock(return_value=[]), codex=self.codex,
                         claude=self.claude, watch_io=Mock(codex_working=Mock(return_value=[]),
                                                          interrupted_threads=Mock(return_value=[])))
        self.s.active = 'C'
        self.s.codex_state['profiles'] = self.codex.quota.return_value['profiles']
        self.s.claude_state['profiles'] = self.claude.quota.return_value['profiles']

    def test_gemini_reset_clears_a_failed_switch_and_force_switches_to_the_first_account(self):
        self.s.transaction = {'phase': 'UNKNOWN', 'selected': 'B'}
        self.s.recovery_required, self.s.cancelled_until = True, 9e12
        self.s.call('provider.reset', {'provider': 'gemini'})
        self.broker.switch.assert_called_once_with('A', close_all=True, force=True)
        self.assertFalse(self.s.recovery_required)
        self.assertEqual(self.s.active, 'A')
        self.assertEqual(self.s.transaction['phase'], 'COMPLETE')
        self.assertIn('reset', [e['kind'] for e in self.s.events])

    def test_codex_reset_switches_to_the_first_account_and_clears_the_cooldown(self):
        self.s.last_codex_switch, self.s.codex_all_exhausted = 9e12, True
        self.s.call('provider.reset', {'provider': 'codex'})
        self.codex.use.assert_called_once_with('CODEX_A')
        self.assertFalse(self.s.codex_all_exhausted)
        self.assertEqual(self.s.codex_state['active'], 'CODEX_A')
        self.codex.open_app.assert_not_called()  # app is running after the switch

    def test_codex_reset_reopens_chatgpt_even_when_the_switch_fails(self):
        self.codex.use.side_effect = QuotaError('CLOSE_TIMEOUT_NOT_SWITCHED')
        self.codex.app_running.return_value = False
        with patch('service.time.sleep'), self.assertRaises(QuotaError):
            self.s.call('provider.reset', {'provider': 'codex'})
        self.codex.open_app.assert_called_once()
        self.assertEqual(self.s.events[-1]['kind'], 'codex_app_reopened')

    def test_claude_reset_switches_to_the_first_account_and_clears_back_off(self):
        self.s.claude_next_poll, self.s.claude_interval = 9e12, 600
        self.s.call('provider.reset', {'provider': 'claude'})
        self.claude.use.assert_called_once_with('CLAUDE_A')
        self.assertEqual((self.s.claude_next_poll, self.s.claude_interval), (0.0, 0))

    def test_reset_is_refused_while_that_tool_is_switching_and_for_unknown_tools(self):
        self.s.codex_state['switching'] = True
        for params in ({'provider': 'codex'}, {'provider': 'x'}):
            with self.assertRaises(QuotaError):
                self.s.call('provider.reset', params)
        self.codex.use.assert_not_called()


class SafetyNetTests(unittest.TestCase):
    def test_reopens_chatgpt_that_was_open_before_and_is_closed_after(self):
        s = ResetTests('run'); s.setUp(); self.addCleanup(s.temp.cleanup)
        s.codex.app_running.return_value = False
        with patch('service.time.sleep'):
            s.s.codex_app_safety_net(True)
        s.codex.open_app.assert_called_once()

    def test_leaves_chatgpt_closed_when_it_was_closed_before(self):
        s = ResetTests('run'); s.setUp(); self.addCleanup(s.temp.cleanup)
        s.codex.app_running.return_value = False
        s.s.codex_app_safety_net(False)
        s.codex.open_app.assert_not_called()


if __name__ == '__main__':
    unittest.main()
