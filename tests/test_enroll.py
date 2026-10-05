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


class EnrollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.busy = Mock(return_value=[])
        self.adder = Mock()
        self.adder.start.side_effect = lambda provider, label: (
            {'url': 'https://accounts.google.com/o/oauth2/auth?x=1'} if provider == 'gemini'
            else {'url': 'https://auth.openai.com/codex/device', 'code': 'ZAXT-QYP2R'})
        self.adder.finish.return_value = {'enrolled': 'GEMINI_F', 'restored': 'GEMINI_C'}
        self.adder.status.return_value = {'state': 'waiting'}
        self.broker = Mock()
        profiles = Mock(list=Mock(return_value={'GEMINI_A': {}, 'GEMINI_C': {}}), active=Mock(return_value='GEMINI_C'))
        client = Mock(fetch=Mock(side_effect=lambda label: row(label)))
        codex = Mock(quota=Mock(return_value={'profiles': {}, 'active': None}))
        self.s = Service(self.temp.name, profiles, client, self.broker, self.busy, codex=codex,
                         watch_io=Mock(codex_working=Mock(return_value=[])), adder=self.adder)

    def test_gemini_start_returns_url_and_blocks_switching(self):
        r = self.s.call('accounts.add.start', {'provider': 'gemini', 'label': 'GEMINI_F'})
        self.assertEqual(r['enrolling']['url'], 'https://accounts.google.com/o/oauth2/auth?x=1')
        self.assertEqual(r['enrolling']['stage'], 'waiting_code')
        with self.assertRaises(QuotaError) as e:
            self.s.call('use', {'label': 'GEMINI_A', 'now': True})
        self.assertEqual(str(e.exception), 'ENROLLMENT_IN_PROGRESS')
        self.broker.switch.assert_not_called()

    def test_gemini_start_refused_while_agy_is_working(self):
        self.busy.return_value = ['agy_print:1']
        with self.assertRaises(QuotaError) as e:
            self.s.call('accounts.add.start', {'provider': 'gemini', 'label': 'GEMINI_F'})
        self.assertEqual(str(e.exception), 'AGY_BUSY_TRY_LATER')
        self.adder.start.assert_not_called()

    def test_gemini_finish_enrolls_and_joins_rotation(self):
        self.s.call('accounts.add.start', {'provider': 'gemini', 'label': 'GEMINI_F'})
        r = self.s.call('accounts.add.finish', {'code': '4/0AXlqoi7-code'})
        self.adder.finish.assert_called_once_with('gemini', '4/0AXlqoi7-code')
        self.assertIsNone(r['enrolling'])
        self.assertIn('GEMINI_F', r['config']['participants'])
        self.assertEqual(self.s.events[-1]['kind'], 'account_added')

    def test_gemini_finish_failure_clears_state_and_records(self):
        self.s.call('accounts.add.start', {'provider': 'gemini', 'label': 'GEMINI_F'})
        self.adder.finish.side_effect = QuotaError('ALREADY_ENROLLED')
        with self.assertRaises(QuotaError):
            self.s.call('accounts.add.finish', {'code': '4/0AXlqoi7-code'})
        self.assertIsNone(self.s.status()['enrolling'])
        self.assertEqual(self.s.events[-1]['kind'], 'account_add_failed')

    def test_codex_login_completes_by_polling(self):
        r = self.s.call('accounts.add.start', {'provider': 'codex', 'label': 'CODEX_C'})
        self.assertEqual(r['enrolling']['code'], 'ZAXT-QYP2R')
        self.s.check_enrollment()
        self.assertEqual(self.s.status()['enrolling']['stage'], 'waiting_device')
        self.adder.status.return_value = {'state': 'done', 'enrolled': 'CODEX_C'}
        self.s.check_enrollment()
        self.assertIsNone(self.s.status()['enrolling'])
        self.assertEqual(self.s.events[-1]['kind'], 'account_added')

    def test_cancel_and_label_validation(self):
        with self.assertRaises(QuotaError):
            self.s.call('accounts.add.start', {'provider': 'gemini', 'label': 'gemini_f'})
        self.s.call('accounts.add.start', {'provider': 'codex', 'label': 'CODEX_C'})
        with self.assertRaises(QuotaError) as e:
            self.s.call('accounts.add.start', {'provider': 'gemini', 'label': 'GEMINI_F'})
        self.assertEqual(str(e.exception), 'ENROLLMENT_IN_PROGRESS')
        r = self.s.call('accounts.add.cancel', {})
        self.adder.cancel.assert_called_once_with('codex')
        self.assertIsNone(r['enrolling'])


class AdderErrorTests(unittest.TestCase):
    def test_close_timeout_while_adding_is_not_reported_as_a_switch(self):
        from enroll import AccountAdder
        proc = Mock(returncode=1, stdout=b'', stderr=b'ERROR: could not stop [10241] within 20s; nothing changed')
        adder = AccountAdder(helpers={'gemini': '/x/agy-account'}, runner=Mock(return_value=proc))
        with self.assertRaises(QuotaError) as e:
            adder._run('gemini', ['login-start', 'GEMINI_H'], 60, 'ADD_FAILED')
        self.assertEqual(str(e.exception), 'CLOSE_TIMEOUT_NOT_ADDED')

if __name__ == '__main__':
    unittest.main()


class RemoveAccountTests(unittest.TestCase):
    def setUp(self):
        EnrollTests.setUp(self)
        self.s.active = 'GEMINI_C'
        self.s.config['participants'] = ['GEMINI_A', 'GEMINI_C']
        self.s.codex_state.update(active='CODEX_A', profiles={'CODEX_A': {'status': 'OK'}, 'CODEX_B': {'status': 'OK'}})
        self.adder.remove.return_value = {'removed': 'x'}

    def test_gemini_remove_drops_it_from_rotation(self):
        r = self.s.call('accounts.remove', {'provider': 'gemini', 'label': 'GEMINI_A'})
        self.adder.remove.assert_called_once_with('gemini', 'GEMINI_A')
        self.assertEqual(r['config']['participants'], ['GEMINI_C'])
        self.assertEqual(self.s.events[-1]['kind'], 'account_removed')

    def test_codex_remove(self):
        self.s.call('accounts.remove', {'provider': 'codex', 'label': 'CODEX_B'})
        self.adder.remove.assert_called_once_with('codex', 'CODEX_B')
        self.assertNotIn('CODEX_B', self.s.codex_state['profiles'])
        self.assertEqual(self.s.events[-1]['kind'], 'codex_account_removed')

    def test_live_account_cannot_be_removed(self):
        for provider, label in (('gemini', 'GEMINI_C'), ('codex', 'CODEX_A')):
            with self.assertRaises(QuotaError) as e:
                self.s.call('accounts.remove', {'provider': provider, 'label': label})
            self.assertEqual(str(e.exception), 'ACCOUNT_IN_USE')
        self.adder.remove.assert_not_called()

    def test_refused_while_adding_or_switching(self):
        self.s.call('accounts.add.start', {'provider': 'codex', 'label': 'CODEX_C'})
        with self.assertRaises(QuotaError) as e:
            self.s.call('accounts.remove', {'provider': 'gemini', 'label': 'GEMINI_A'})
        self.assertEqual(str(e.exception), 'ENROLLMENT_IN_PROGRESS')
        self.adder.remove.assert_not_called()
