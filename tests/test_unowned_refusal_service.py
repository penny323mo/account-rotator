import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from service import Service
from quota import QuotaError
from test_priority import grow, H
from test_warmup import codex  # noqa: F401


class UnownedRefusalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {k: {} for k in 'ABC'}
        profiles.active.return_value = 'C'
        self.broker = Mock()
        self.codex = Mock()
        self.codex.quota.return_value = {'profiles': {}, 'active': None}
        self.s = Service(self.temp.name, profiles, Mock(), self.broker, Mock(return_value=[]), codex=self.codex,
                         watch_io=Mock(codex_working=Mock(return_value=[]), interrupted_threads=Mock(return_value=[])))
        self.s.config['participants'] = list('ABC')
        self.s.set_enabled(True)
        self.s.active = 'C'
        self.s.rows = {'C': grow('C', 1, 40), 'A': grow('A', 36, 40), 'B': grow('B', 100, 46)}

    def fail_with(self, code):
        self.broker.switch.side_effect = QuotaError(code)
        now = time.time()
        self.s.tick(now)
        self.broker.switch.assert_called_once()
        return self.s.cancelled_until - now

    def test_unowned_agy_refusal_retries_in_120s_not_cooldown(self):
        wait = self.fail_with('UNOWNED_AGY_NOT_SWITCHED')
        self.assertTrue(100 < wait < 130, wait)
        self.assertEqual(self.s.transaction['phase'], 'FAILED')
        self.assertFalse(self.s.recovery_required)

    def test_drain_timeout_retries_in_at_least_120s(self):
        """A long production --print job must not be fenced again every ~50 s."""
        for code in ('CLOSE_TIMEOUT_NOT_SWITCHED', 'AGY_RUNNING_NOT_SWITCHED'):
            self.broker.switch.reset_mock()
            self.s.cancelled_until = 0
            self.s.last_auto_attempt = 0
            self.assertGreaterEqual(self.fail_with(code), 115, code)

    def test_other_failures_keep_the_600s_cooldown(self):
        wait = self.fail_with('ACTIVATION_FAILED_ROLLED_BACK')
        self.assertGreater(wait, 500)


if __name__ == '__main__':
    unittest.main()
