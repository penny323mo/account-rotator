import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401  (no real Codex/agy/Claude helpers in tests)
from claude import ClaudeAccounts
from quota import QuotaError
from service import Service, WARM_DELAY

H = 3600


def claude_row(remaining, reset_in, active=False):
    reset = None if reset_in is None else time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + reset_in))
    return {'status': 'OK', 'active': active, 'email': 'x@y', 'plan': 'pro',
            '5h': {'used_percent': 100 - remaining, 'remaining_percent': remaining, 'reset_at': reset},
            'weekly': {'used_percent': 40, 'remaining_percent': 60, 'reset_at': None}}


class ClaudeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.claude = Mock()
        self.claude.quota.return_value = {'profiles': {'CLAUDE_A': claude_row(33, 2 * H, active=True),
                                                       'CLAUDE_B': claude_row(100, None)}, 'active': 'CLAUDE_A'}
        self.adder = Mock()
        profiles = Mock(list=Mock(return_value={}), active=Mock(return_value=None))
        self.codex = Mock(quota=Mock(return_value={'profiles': {}, 'active': None}))
        self.s = Service(self.temp.name, profiles, Mock(), Mock(), Mock(return_value=[]), claude=self.claude,
                         codex=self.codex, adder=self.adder, watch_io=Mock(codex_working=Mock(return_value=[])))
        self.s.warmup['gemini'] = self.s.warmup['codex'] = False

    def test_status_carries_claude_usage(self):
        self.s.refresh_claude(0)
        c = self.s.status()['claude']
        self.assertEqual((c['active'], c['profiles']['CLAUDE_A']['5h']['remaining_percent']), ('CLAUDE_A', 33))

    def test_window_not_started_has_no_reset_time_and_is_warmed(self):
        self.s.refresh_claude(0)
        now = time.time()
        self.s.warm_idle(now)
        self.s.warm_idle(now + WARM_DELAY + 1)
        self.claude.warm.assert_called_once_with('CLAUDE_B')
        self.assertEqual(self.s.events[-1]['kind'], 'claude_warmed')

    def test_counting_or_used_windows_are_not_warmed(self):
        self.claude.quota.return_value = {'profiles': {'CLAUDE_A': claude_row(33, 2 * H, active=True)}, 'active': 'CLAUDE_A'}
        self.s.refresh_claude(0)
        self.s.warm_idle(time.time() + WARM_DELAY + 1)
        self.claude.warm.assert_not_called()

    def test_switch_off_stops_claude_warmups(self):
        self.s.refresh_claude(0)
        self.s.call('warmup.set', {'provider': 'claude', 'enabled': False})
        now = time.time()
        self.s.warm_idle(now)
        self.s.warm_idle(now + WARM_DELAY + 1)
        self.claude.warm.assert_not_called()

    def test_live_account_cannot_be_removed_idle_one_can(self):
        self.s.refresh_claude(0)
        with self.assertRaises(QuotaError):
            self.s.call('accounts.remove', {'provider': 'claude', 'label': 'CLAUDE_A'})
        self.s.call('accounts.remove', {'provider': 'claude', 'label': 'CLAUDE_B'})
        self.adder.remove.assert_called_once_with('claude', 'CLAUDE_B')
        self.assertEqual(self.s.events[-1]['kind'], 'claude_account_removed')

    def test_add_account_is_a_paste_the_code_sign_in(self):
        self.adder.start.return_value = {'label': 'CLAUDE_B', 'url': 'https://claude.com/cai/oauth/authorize?x'}
        self.adder.finish.return_value = {'enrolled': 'CLAUDE_B'}
        st = self.s.call('accounts.add.start', {'provider': 'claude', 'label': 'CLAUDE_B'})
        self.assertEqual((st['enrolling']['stage'], st['enrolling']['url'][:22]), ('waiting_code', 'https://claude.com/cai'))
        self.s.call('accounts.add.finish', {'code': 'abc#def12345'})
        self.adder.finish.assert_called_once_with('claude', 'abc#def12345')
        self.assertEqual(self.s.events[-1]['kind'], 'account_added')
        self.assertIsNone(self.s.status()['enrolling'])

    def test_switch_reconnects_remote_control_and_logs_each_session(self):
        import threading
        done = threading.Event()
        def run(report, before=None):
            self.assertEqual(before, {1: {'name': 'Clawbook'}})  # noted before the switch
            report({'name': 'Clawbook'}, 'session_new', None)
            report({'name': 'Room'}, None, 'NO_TERMINAL')
            done.set()
        self.s.relinker = Mock(run=run, remote_sessions=Mock(return_value={1: {'name': 'Clawbook'}}),
                               stopped_sessions=Mock(return_value={}))
        self.s.refresh_claude(0)
        self.claude.use.return_value = {'selected': 'CLAUDE_B', 'activation': 'OK'}
        self.s.call('claude.use', {'label': 'CLAUDE_B'})
        self.assertTrue(done.wait(2))
        kinds = [(e['kind'], e.get('project'), e.get('error')) for e in self.s.events]
        self.assertIn(('claude_remote_relinked', 'Clawbook', None), kinds)
        self.assertIn(('claude_remote_failed', 'Room', 'NO_TERMINAL'), kinds)

    def test_manual_switch_records_and_marks_the_new_live_account(self):
        self.s.refresh_claude(0)
        self.claude.use.return_value = {'selected': 'CLAUDE_B', 'previous': 'CLAUDE_A'}
        self.s.call('claude.use', {'label': 'CLAUDE_B'})
        self.claude.use.assert_called_once_with('CLAUDE_B')
        c = self.s.status()['claude']
        self.assertEqual((c['active'], c['profiles']['CLAUDE_B']['active'], c['switching']), ('CLAUDE_B', True, False))
        self.assertEqual(self.s.events[-1]['kind'], 'claude_switched')

    def test_failed_switch_is_recorded_and_unlocked(self):
        self.s.refresh_claude(0)
        self.claude.use.side_effect = QuotaError('CLAUDE_ACTIVATION_ROLLED_BACK')
        with self.assertRaises(QuotaError):
            self.s.call('claude.use', {'label': 'CLAUDE_B'})
        self.assertEqual(self.s.events[-1]['kind'], 'claude_switch_failed')
        self.assertFalse(self.s.status()['claude']['switching'])
        self.assertTrue(self.s.claude_lock.acquire(blocking=False))

    def test_unknown_label_is_refused(self):
        with self.assertRaises(QuotaError):
            self.s.call('claude.use', {'label': 'CLAUDE_Z'})

    def limited(self):
        return {'profiles': {'CLAUDE_A': {'status': 'HTTP_429', 'active': True, 'email': 'x@y'},
                             'CLAUDE_B': {'status': 'HTTP_429', 'active': False}}, 'active': 'CLAUDE_A'}

    def test_polls_every_poll_until_rate_limited(self):
        self.s.refresh_claude(1000)
        self.s.refresh_claude(1030)  # inside the 60 s base interval
        self.assertEqual(self.claude.quota.call_count, 1)
        self.s.refresh_claude(1060)
        self.assertEqual(self.claude.quota.call_count, 2)

    def test_rate_limit_keeps_the_last_numbers_and_slows_down_step_by_step(self):
        good = self.claude.quota.return_value
        self.s.refresh_claude(1000)
        self.claude.quota.return_value = self.limited()
        self.s.refresh_claude(1060)
        a = self.s.status()['claude']['profiles']['CLAUDE_A']
        self.assertEqual((a['status'], a['5h']['remaining_percent'], a['stale']), ('OK', 33, 'HTTP_429'))
        self.assertEqual(self.s.claude_interval, 120)
        self.s.refresh_claude(1060 + 119)
        self.assertEqual(self.claude.quota.call_count, 2)  # waiting out the longer interval
        self.s.refresh_claude(1060 + 121)                  # limited again: 240, 480, then capped at 600
        self.s.refresh_claude(1060 + 121 + 241)
        self.s.refresh_claude(1060 + 121 + 241 + 481)
        self.assertEqual(self.s.claude_interval, 600)
        self.claude.quota.return_value = good
        self.s.refresh_claude(1060 + 121 + 241 + 481 + 601)
        a = self.s.status()['claude']['profiles']['CLAUDE_A']
        self.assertEqual((a['5h']['remaining_percent'], a.get('stale')), (33, None))

    def test_interval_speeds_up_again_after_ten_good_polls_in_a_row(self):
        self.s.claude_interval = 240
        t = 1000
        for _ in range(10):
            self.s.refresh_claude(t)
            t += 241
        self.assertEqual(self.s.claude_interval, 120)

    def test_manual_refresh_of_claude_asks_even_inside_the_interval(self):
        self.s.refresh_claude(1000)
        self.s.refresh_claude(1010, force=True)
        self.assertEqual(self.claude.quota.call_count, 2)

    def test_stale_numbers_are_never_used_to_warm(self):
        self.s.refresh_claude(1000)
        self.claude.quota.return_value = self.limited()
        self.s.refresh_claude(1060)
        now = time.time()
        self.s.warm_idle(now)
        self.s.warm_idle(now + WARM_DELAY + 1)
        self.claude.warm.assert_not_called()

    def test_first_ever_failure_still_shows_as_failed(self):
        self.claude.quota.return_value = {'profiles': {'CLAUDE_A': {'status': 'HTTP_429', 'active': True}}, 'active': 'CLAUDE_A'}
        self.s.refresh_claude(1000)
        self.assertEqual(self.s.status()['claude']['profiles']['CLAUDE_A']['status'], 'HTTP_429')

    def test_refresh_is_scoped_to_the_tab_that_asked(self):
        for provider in ('claude', 'codex'):
            self.claude.quota.reset_mock(); self.codex.quota.reset_mock(); self.s.client.fetch.reset_mock()
            self.s.refresh(provider)
            self.assertEqual(self.s.client.fetch.call_count, 0)  # Antigravity quotas are not touched
            self.assertEqual((self.claude.quota.call_count, self.codex.quota.call_count),
                             (1, 0) if provider == 'claude' else (0, 1))

    def test_refresh_rpc_remembers_the_scope_and_rejects_unknown_ones(self):
        self.s.call('refresh', {'provider': 'claude'})
        self.assertEqual(self.s.refresh_scope, 'claude')
        with self.assertRaises(QuotaError):
            self.s.call('refresh', {'provider': 'bing'})
        self.s.call('refresh', {})
        self.assertIsNone(self.s.refresh_scope)

    def test_status_names_the_suggested_claude_candidate(self):
        self.s.refresh_claude(0)
        self.assertEqual(self.s.status()['claude']['recommended'], 'CLAUDE_B')  # the only other account, full window

    def test_helper_failure_is_an_error_not_a_crash(self):
        self.claude.quota.side_effect = QuotaError('CLAUDE_QUOTA_UNAVAILABLE')
        self.s.refresh_claude(0)
        self.assertEqual(self.s.status()['claude']['error'], 'CLAUDE_QUOTA_UNAVAILABLE')


class ClaudeAccountsTests(unittest.TestCase):
    def test_quota_reads_the_helper_json(self):
        out = json.dumps({'profiles': {'CLAUDE_A': {'status': 'OK', 'active': True}}}).encode()
        c = ClaudeAccounts(helper='/x/claude-account', runner=Mock(return_value=types.SimpleNamespace(returncode=0, stdout=out)))
        self.assertEqual(c.quota()['active'], 'CLAUDE_A')

    def test_use_maps_a_rolled_back_switch(self):
        out = json.dumps({'activation': 'ROLLED_BACK'}).encode()
        c = ClaudeAccounts(helper='/x/claude-account', runner=Mock(return_value=types.SimpleNamespace(returncode=2, stdout=out, stderr=b'')))
        with self.assertRaises(QuotaError) as e:
            c.use('CLAUDE_B')
        self.assertEqual(str(e.exception), 'CLAUDE_ACTIVATION_ROLLED_BACK')

    def test_failed_helper_is_quota_unavailable(self):
        c = ClaudeAccounts(helper='/x/claude-account', runner=Mock(return_value=types.SimpleNamespace(returncode=1, stdout=b'')))
        with self.assertRaises(QuotaError):
            c.quota()


if __name__ == '__main__':
    unittest.main()


class ClaudeAutoContinueTests(ClaudeServiceTests.__base__):
    """Sessions stopped on the usage limit get "continue" once quota is back."""
    def setUp(self):
        ClaudeServiceTests.setUp(self)
        self.record = {'pid': 7, 'sessionId': 'sess-7', 'name': 'Control room', 'status': 'idle'}
        self.stopped = {7: (self.record, 'err-1')}
        self.s.relinker = Mock(stopped_sessions=Mock(side_effect=lambda: dict(self.stopped)),
                               resume=Mock(return_value=None), io=Mock(), remote_sessions=Mock(return_value={}),
                               run=Mock(), last_error=Mock(return_value=None))

    def quota(self, a, b):
        self.claude.quota.return_value = {'profiles': {'CLAUDE_A': {**claude_row(a, 2 * H, active=True)},
                                                       'CLAUDE_B': claude_row(b, 3 * H)}, 'active': 'CLAUDE_A'}

    def wait_idle(self):
        for _ in range(200):
            if not self.s.claude_continuing:
                return
            time.sleep(0.01)

    def kinds(self):
        return [e['kind'] for e in self.s.events]

    def test_live_account_with_quota_continues_the_stopped_session(self):
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.relinker.resume.assert_called_once_with(self.record)
        self.assertIn('claude_autocontinue', self.kinds())
        self.claude.use.assert_not_called()

    def test_continue_counts_only_once_the_session_answers(self):
        self.s.relinker.last_reply = Mock(return_value=('err-1', True))  # nothing new yet
        self.s.refresh_claude(0)
        self.wait_idle()
        self.stopped = {}  # "continue" typed: the last message is no longer the error
        self.s.refresh_claude(0, force=True)
        self.assertNotIn('claude_continue_done', self.kinds())
        self.s.relinker.last_reply.return_value = ('reply-1', False)
        self.s.refresh_claude(0, force=True)
        self.assertEqual(self.kinds().count('claude_continue_done'), 1)
        self.s.refresh_claude(0, force=True)
        self.assertEqual(self.kinds().count('claude_continue_done'), 1)

    def test_limit_again_or_silence_is_reported_not_done(self):
        self.s.relinker.last_reply = Mock(return_value=('err-2', True))
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.refresh_claude(0, force=True)
        self.assertIn('claude_continue_limited', self.kinds())
        self.assertNotIn('claude_continue_done', self.kinds())

    def test_an_error_reply_is_reported_not_done(self):
        self.s.relinker.last_reply = Mock(return_value=('err-1', True))
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.relinker.last_reply.return_value = ('api-err', False)
        self.s.relinker.last_error.return_value = {'uuid': 'api-err', 'error': 'server_error',
                                                   'text': 'API Error: 529 Overloaded.', 'timestamp': 't'}
        self.s.refresh_claude(0, force=True)
        self.assertIn('claude_continue_error', self.kinds())
        self.assertNotIn('claude_continue_done', self.kinds())

    def test_a_session_stopped_on_an_error_gets_continue_five_minutes_later(self):
        from datetime import datetime, timezone
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.relinker.resume.reset_mock()
        stopped = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat()
        self.s.watch_io = Mock(recent_threads=Mock(return_value=[]))
        self.s.relinker.sessions = Mock(return_value=[(self.record, False, 0)])
        self.s.relinker.last_error.return_value = {'uuid': 'api-err', 'error': 'server_error',
                                                   'text': 'API Error: 529 Overloaded.', 'timestamp': stopped}
        self.s.check_session_errors(time.time())
        self.wait_idle()
        self.s.relinker.resume.assert_called_once_with(self.record)
        self.assertIn('claude_session_error', self.kinds())
        self.assertIn('claude_errorretry', self.kinds())

    def test_no_answer_for_half_an_hour_is_reported(self):
        self.s.relinker.last_reply = Mock(return_value=('err-1', True))
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.claude_check_replies(time.time() + 31 * 60)
        self.assertIn('claude_continue_noreply', self.kinds())

    def test_page_button_continues_now_even_with_auto_continue_off(self):
        self.s.call('claude.waitcontinue.set', {'enabled': False})
        self.s.refresh_claude(0)
        self.assertEqual(self.s.status()['claude']['stopped'], ['Control room'])
        self.s.call('claude.continue.now', {})
        self.wait_idle()
        self.s.relinker.resume.assert_called_once_with(self.record)
        self.assertIn('claude_manualcontinue', self.kinds())

    def test_page_button_sends_the_users_own_text(self):
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.relinker.resume.reset_mock()
        self.s.relinker.stopped_sessions = Mock(return_value={7: (self.record, 'err-2')})
        self.s.call('claude.continue.now', {'text': '睇返個 log 再繼續'})
        self.wait_idle()
        self.s.relinker.resume.assert_called_once_with(self.record, '睇返個 log 再繼續')

    def test_page_button_keeps_talking_to_the_session_it_continued(self):
        self.s.call('claude.waitcontinue.set', {'enabled': False})
        self.s.refresh_claude(0)
        self.s.relinker.session = Mock(return_value=(self.record, False))  # continued, working
        self.s.call('claude.continue.now', {})
        self.wait_idle()
        other = {'pid': 8, 'sessionId': 'sess-8', 'name': 'Other', 'status': 'idle'}
        self.stopped = {8: (other, 'err-9')}  # another session stopped meanwhile
        self.s.call('claude.continue.now', {'text': '跟住做'})
        self.wait_idle()
        self.assertEqual(self.s.relinker.resume.call_args_list[-1][0], (self.record, '跟住做'))
        self.s.relinker.session.return_value = (self.record, True)  # it hit the limit again
        self.s.claude_target['at'] = 0
        self.s.call('claude.continue.now', {})
        self.wait_idle()
        self.assertEqual(self.s.relinker.resume.call_args_list[-1][0][0], other)

    def test_picker_and_a_picked_session(self):
        other = {'pid': 8, 'sessionId': 'sess-8', 'name': 'Control room', 'status': 'idle'}
        self.s.relinker.sessions = Mock(return_value=[(self.record, False, 5.0), (other, False, 9.0)])
        self.s.relinker.session = Mock(side_effect=lambda sid: (other, False) if sid == 'sess-8' else None)
        self.stopped = {}
        self.s.refresh_claude(0)  # live account's quota known
        r = self.s.call('continue.sessions', {'provider': 'claude'})
        self.assertEqual([i['id'] for i in r['items']], ['sess-8', 'sess-7'])
        self.assertEqual(r['default'], 'sess-8')  # nothing stopped, no target: the most recent one
        self.s.call('claude.continue.now', {'session': 'sess-8', 'text': '你好'})
        self.wait_idle()
        self.s.relinker.resume.assert_called_once_with(other, '你好')
        self.assertEqual(self.s.claude_target['sessionId'], 'sess-8')
        again = Service(self.temp.name, Mock(list=Mock(return_value={}), active=Mock(return_value=None)), Mock(),
                        Mock(), Mock(return_value=[]), claude=self.claude, relinker=self.s.relinker)
        self.assertEqual(again.claude_target['sessionId'], 'sess-8')  # kept across a restart

    def test_page_button_refused_while_the_live_account_is_used_up(self):
        self.quota(0, 70)
        self.s.call('claude.waitcontinue.set', {'enabled': False})
        self.s.refresh_claude(0)
        with self.assertRaises(QuotaError) as e:
            self.s.call('claude.continue.now', {})
        self.assertEqual(str(e.exception), 'ACTIVE_EXHAUSTED')
        self.s.relinker.resume.assert_not_called()

    def test_same_stop_is_never_continued_twice(self):
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.refresh_claude(0, force=True)
        self.wait_idle()
        self.assertEqual(self.s.relinker.resume.call_count, 1)

    def test_used_up_live_account_waits_then_continues_when_its_window_refills(self):
        self.quota(0, 70)  # another account has quota, but Claude never switches by itself
        self.s.refresh_claude(0)
        self.s.refresh_claude(0, force=True)
        self.assertEqual(self.kinds().count('claude_waiting_quota'), 1)
        self.claude.use.assert_not_called()
        self.s.relinker.resume.assert_not_called()
        self.quota(40, 70)  # the live account's 5 h window refilled
        self.s.refresh_claude(0, force=True)
        self.wait_idle()
        self.s.relinker.resume.assert_called_once_with(self.record)

    def test_manual_switch_never_continues_automatically(self):
        # a switch by hand does nothing automatic; the ▶ button is there for that
        self.quota(0, 70)
        self.s.refresh_claude(0)
        self.claude.use.return_value = {'selected': 'CLAUDE_B', 'previous': 'CLAUDE_A'}
        self.s.call('claude.use', {'label': 'CLAUDE_B'})
        for _ in range(200):
            if not self.s.claude_relinking:
                break
            time.sleep(0.01)
        self.claude.quota.return_value = {'profiles': {'CLAUDE_A': claude_row(0, 2 * H),
                                                       'CLAUDE_B': claude_row(70, 3 * H, active=True)}, 'active': 'CLAUDE_B'}
        self.s.refresh_claude(0, force=True)
        self.s.refresh_claude(10 ** 10, force=True)
        self.wait_idle()
        self.s.relinker.resume.assert_not_called()
        self.assertIn('claude_waiting_cleared', self.kinds())
        self.s.call('claude.continue.now', {})  # the button still works
        self.wait_idle()
        self.s.relinker.resume.assert_called_once_with(self.record)

    def test_session_continued_by_hand_meanwhile_is_left_alone(self):
        self.quota(0, 50)
        self.s.refresh_claude(0)
        self.stopped = {}
        self.quota(40, 50)
        self.s.refresh_claude(0, force=True)
        self.wait_idle()
        self.s.relinker.resume.assert_not_called()

    def test_failure_to_type_is_reported(self):
        self.s.relinker.resume.return_value = 'PROMPT_NOT_READY'
        self.s.refresh_claude(0)
        self.wait_idle()
        self.assertEqual((self.s.events[-1]['kind'], self.s.events[-1]['error']),
                         ('claude_autocontinue_failed', 'PROMPT_NOT_READY'))

    def test_off_does_nothing(self):
        self.s.call('claude.waitcontinue.set', {'enabled': False})
        self.s.refresh_claude(0)
        self.wait_idle()
        self.s.relinker.resume.assert_not_called()  # read for the page button only, nothing typed
