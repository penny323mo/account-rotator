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


class AdderFailureTests(unittest.TestCase):
    def test_signing_in_again_as_another_account_is_named(self):
        from enroll import AccountAdder
        proc = Mock(returncode=1, stderr=b'ERROR: that is not the account enrolled as CLAUDE_C\n', stdout=b'')
        adder = AccountAdder(helpers={'claude': '/x'}, runner=Mock(return_value=proc))
        with self.assertRaises(QuotaError) as e:
            adder.finish('claude', 'code#state')
        self.assertEqual(str(e.exception), 'WRONG_ACCOUNT')


class RenameAccountTests(unittest.TestCase):
    def setUp(self):
        RemoveAccountTests.setUp(self)
        self.s.rows = {'GEMINI_A': {'label': 'GEMINI_A', 'status': 'OK', 'groups': []},
                       'GEMINI_C': {'label': 'GEMINI_C', 'status': 'OK', 'groups': []}}
        self.s.config['participants'] = ['GEMINI_C', 'GEMINI_A']
        self.adder.rename.return_value = {'renamed': 'x'}

    def rename(self, provider, label, new):
        return self.s.call('accounts.rename', {'provider': provider, 'label': label, 'new': new})

    def test_gemini_keeps_its_place_in_rotation_and_in_the_gemini_app(self):
        apps = Path(self.temp.name) / 'gemini_app.json'
        apps.write_text(json.dumps({'user1': 'GEMINI_C', 'user2': 'GEMINI_A'}))
        r = self.rename('gemini', 'GEMINI_A', 'GEMINI_B')
        self.adder.rename.assert_called_once_with('gemini', 'GEMINI_A', 'GEMINI_B')
        self.assertEqual(r['config']['participants'], ['GEMINI_C', 'GEMINI_B'])
        self.assertEqual(self.s.rows['GEMINI_B']['label'], 'GEMINI_B')
        self.assertNotIn('GEMINI_A', self.s.rows)
        self.assertEqual(json.loads(apps.read_text()), {'user1': 'GEMINI_C', 'user2': 'GEMINI_B'})
        self.assertEqual((self.s.events[-1]['kind'], self.s.events[-1]['previous']), ('account_renamed', 'GEMINI_A'))

    def test_codex_numbers_move_to_the_new_label(self):
        self.s.warm_last[('codex', 'CODEX_B')] = 5.0
        self.rename('codex', 'CODEX_B', 'CODEX_C')
        self.assertEqual(sorted(self.s.codex_state['profiles']), ['CODEX_A', 'CODEX_C'])
        self.assertEqual(self.s.warm_last, {('codex', 'CODEX_C'): 5.0})
        self.assertEqual(self.s.events[-1]['kind'], 'codex_account_renamed')

    def test_live_taken_or_bad_names_are_refused_before_the_helper(self):
        for provider, label, new, error in (('gemini', 'GEMINI_C', 'GEMINI_D', 'ACCOUNT_IN_USE'),
                                            ('codex', 'CODEX_B', 'CODEX_A', 'LABEL_TAKEN'),
                                            ('codex', 'CODEX_B', 'GEMINI_D', 'INVALID_LABEL'),
                                            ('codex', 'CODEX_B', 'CODEX_B', 'INVALID_LABEL'),
                                            ('claude', 'CLAUDE_B', 'CLAUDE_X; rm', 'INVALID_LABEL')):
            with self.assertRaises(QuotaError) as e:
                self.rename(provider, label, new)
            self.assertEqual(str(e.exception), error)
        self.adder.rename.assert_not_called()
