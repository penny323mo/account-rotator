import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from codex import CodexAccounts
from quota import QuotaError
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import Service
from test_service import row

QUOTA = {'profiles': {
    'CODEX_A': {'status': 'OK', 'plan': 'plus', 'active': True,
                '5h': {'used_percent': 2, 'remaining_percent': 98, 'reset_at': 1790454707},
                'weekly': {'used_percent': 16, 'remaining_percent': 84, 'reset_at': 1791021866}},
    'CODEX_B': {'status': 'TOKEN_REVOKED', 'active': False}}}


def proc(code=0, stdout=b'', stderr=b''):
    return types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class CodexAccountsTests(unittest.TestCase):
    def test_quota_delegates_to_helper_and_reports_active(self):
        runner = Mock(return_value=proc(stdout=json.dumps(QUOTA).encode()))
        data = CodexAccounts(Path('/h/codex-account'), runner).quota()
        self.assertEqual(runner.call_args.args[0], ['/h/codex-account', '--json', 'quota'])
        self.assertEqual(data['active'], 'CODEX_A')
        self.assertEqual(data['profiles']['CODEX_B']['status'], 'TOKEN_REVOKED')

    def test_quota_failure_is_a_constant_code(self):
        runner = Mock(return_value=proc(1, stderr=b'Traceback ... secret'))
        with self.assertRaises(QuotaError) as e:
            CodexAccounts(Path('/h/x'), runner).quota()
        self.assertEqual(str(e.exception), 'CODEX_QUOTA_UNAVAILABLE')

    def test_missing_helper_means_not_configured(self):
        with self.assertRaises(QuotaError) as e:
            CodexAccounts(Path('/nonexistent/codex-account')).quota()
        self.assertEqual(str(e.exception), 'CODEX_NOT_CONFIGURED')

    def test_use_closes_codex_and_app_first(self):
        runner = Mock(return_value=proc(stdout=b'{"activation":"OK","selected":"CODEX_B","previous":"CODEX_A","app_reopened":true}'))
        result = CodexAccounts(Path('/h/codex-account'), runner).use('CODEX_B')
        self.assertEqual(runner.call_args.args[0], ['/h/codex-account', '--json', 'use', 'CODEX_B', '--close-all'])
        self.assertGreaterEqual(runner.call_args.kwargs['timeout'], 60)
        self.assertTrue(result['app_reopened'])

    def test_use_maps_helper_refusals(self):
        cases = [(proc(stdout=b'{"activation":"OK","selected":"CODEX_B","previous":"CODEX_A","codex_running":[1]}'), None),
                 (proc(2, stdout=b'{"activation":"ROLLED_BACK"}'), 'CODEX_ACTIVATION_ROLLED_BACK'),
                 (proc(2, stdout=b'{"activation":"ROLLBACK_FAILED"}'), 'CODEX_ROLLBACK_FAILED_CHECK_CURRENT'),
                 (proc(1, stderr=b'ERROR: live Codex login is not enrolled; run ...'), 'CODEX_LIVE_NOT_ENROLLED'),
                 (proc(1, stderr=b'ERROR: another account switch is in progress'), 'SWITCH_BUSY'),
                 (proc(1, stderr=b'ERROR: stored profile CODEX_B is missing or malformed'), 'CODEX_PROFILE_BROKEN'),
                 (proc(1, stderr=b'ERROR: could not stop [20] within 30s; live login untouched'), 'CLOSE_TIMEOUT_NOT_SWITCHED'),
                 (proc(1, stderr=b'boom'), 'CODEX_SWITCH_FAILED')]
        for result, code in cases:
            accounts = CodexAccounts(Path('/h/codex-account'), Mock(return_value=result))
            if code is None:
                self.assertEqual(accounts.use('CODEX_B')['selected'], 'CODEX_B')
            else:
                with self.assertRaises(QuotaError) as e:
                    accounts.use('CODEX_B')
                self.assertEqual(str(e.exception), code)


class ServiceCodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {'A': {'id': 'a'}}
        profiles.active.return_value = 'A'
        client = Mock()
        client.fetch.side_effect = lambda label: row(label)
        self.codex = Mock()
        self.codex.quota.return_value = json.loads(json.dumps({**QUOTA, 'active': 'CODEX_A'}))
        self.codex.use.side_effect = lambda label: {'activation': 'OK', 'selected': label, 'codex_running': [1]}
        self.s = Service(self.temp.name, profiles, client, Mock(), Mock(return_value=[]), codex=self.codex)

    def test_refresh_includes_codex_accounts(self):
        self.s.refresh()
        codex = self.s.status()['codex']
        self.assertEqual(codex['active'], 'CODEX_A')
        self.assertEqual(codex['profiles']['CODEX_A']['5h']['remaining_percent'], 98)
        self.assertIsNotNone(codex['updated_at'])
        self.assertIsNone(codex['error'])

    def test_codex_failure_does_not_break_gemini_refresh(self):
        self.codex.quota.side_effect = QuotaError('CODEX_QUOTA_UNAVAILABLE')
        self.s.refresh()
        status = self.s.status()
        self.assertEqual(status['profiles']['A']['status'], 'OK')
        self.assertEqual(status['codex']['error'], 'CODEX_QUOTA_UNAVAILABLE')

    def test_codex_use_switches_and_records_event(self):
        self.s.refresh()
        result = self.s.call('codex.use', {'label': 'CODEX_B'})
        self.codex.use.assert_called_once_with('CODEX_B')
        self.assertEqual(result['selected'], 'CODEX_B')
        self.assertEqual(self.s.status()['codex']['active'], 'CODEX_B')
        self.assertEqual(self.s.events[-1]['kind'], 'codex_switched')

    def test_codex_use_rejects_unknown_label(self):
        self.s.refresh()
        with self.assertRaises(QuotaError):
            self.s.call('codex.use', {'label': 'NOPE'})
        self.codex.use.assert_not_called()

    def test_codex_failure_is_recorded(self):
        self.s.refresh()
        self.codex.use.side_effect = QuotaError('CODEX_LIVE_NOT_ENROLLED')
        with self.assertRaises(QuotaError):
            self.s.call('codex.use', {'label': 'CODEX_B'})
        self.assertEqual(self.s.events[-1], {**self.s.events[-1], 'kind': 'codex_switch_failed',
                                             'error': 'CODEX_LIVE_NOT_ENROLLED'})
        self.assertFalse(self.s.status()['codex']['switching'])


def window(remaining):
    return {'used_percent': 100 - remaining, 'remaining_percent': remaining, 'reset_at': 1790454707}


def codex_row(five=80, weekly=80, status='OK', limit=False, active=False):
    return {'status': status, 'plan': 'plus', 'limit_reached': limit, 'active': active,
            '5h': window(five), 'weekly': window(weekly)} if status == 'OK' else {'status': status, 'active': active}


class CodexAutoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {'A': {'id': 'a'}}
        profiles.active.return_value = 'A'
        self.client = Mock()
        self.client.fetch.side_effect = lambda label: row(label)
        self.codex = Mock()
        self.rows = {'CODEX_A': codex_row(five=0, active=True), 'CODEX_B': codex_row(five=60, weekly=40)}
        self.codex.quota.side_effect = lambda: {'profiles': json.loads(json.dumps(self.rows)),
                                                'active': next(k for k, r in self.rows.items() if r['active'])}
        self.codex.use.side_effect = lambda label: {'selected': label, 'previous': None, 'codex_running': [], 'app_reopened': True}
        self.s = self.service()

    def service(self):
        return Service(self.temp.name, Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A')),
                       self.client, Mock(), Mock(return_value=[]), codex=self.codex)

    def test_off_by_default_never_switches(self):
        self.s.refresh_codex()
        self.assertFalse(self.s.status()['codex']['auto'])
        self.codex.use.assert_not_called()

    def test_exhausted_active_switches_to_account_with_capacity(self):
        self.s.call('codex.auto.set', {'enabled': True})
        self.s.refresh_codex()
        self.codex.use.assert_called_once_with('CODEX_B')
        self.assertEqual(self.s.events[-1]['kind'], 'codex_switched')
        self.assertEqual(self.s.events[-1]['source'], 'automatic')

    def test_weekly_zero_and_limit_flag_also_count_as_exhausted(self):
        self.s.call('codex.auto.set', {'enabled': True})
        for active in (codex_row(weekly=0, active=True), codex_row(limit=True, active=True)):
            self.rows['CODEX_A'] = active
            self.s.last_codex_switch = 0
            self.codex.use.reset_mock()
            self.s.refresh_codex()
            self.codex.use.assert_called_once_with('CODEX_B')

    def test_not_exhausted_does_not_switch(self):
        self.s.call('codex.auto.set', {'enabled': True})
        self.rows['CODEX_A'] = codex_row(five=3, weekly=2, active=True)
        self.s.refresh_codex()
        self.codex.use.assert_not_called()

    def test_no_candidate_when_other_is_zero_or_broken(self):
        self.s.call('codex.auto.set', {'enabled': True})
        for other in (codex_row(weekly=0), codex_row(limit=True), codex_row(status='TOKEN_REVOKED')):
            self.rows['CODEX_B'] = other
            self.s.refresh_codex()
        self.codex.use.assert_not_called()
        kinds = [e['kind'] for e in self.s.events]
        self.assertEqual(kinds.count('codex_all_exhausted'), 1)

    def test_cooldown_prevents_ping_pong(self):
        self.s.call('codex.auto.set', {'enabled': True})
        self.s.refresh_codex()
        self.rows = {'CODEX_A': codex_row(five=50), 'CODEX_B': codex_row(five=0, active=True)}
        self.s.refresh_codex()
        self.codex.use.assert_called_once_with('CODEX_B')

    def test_toggle_persists_and_validates(self):
        self.s.call('codex.auto.set', {'enabled': True})
        self.assertTrue(self.service().status()['codex']['auto'])
        with self.assertRaises(QuotaError):
            self.s.call('codex.auto.set', {'enabled': 'yes'})
        self.assertEqual([e['kind'] for e in self.s.events].count('codex_auto_changed'), 1)


if __name__ == '__main__':
    unittest.main()
