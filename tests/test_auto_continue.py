import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from quota import QuotaError, stamp
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import Service, reset_epoch
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

    def test_turning_autocontinue_on_turns_auto_switch_on_too(self):
        self.s.call('codex.auto.set', {'enabled': False})
        self.s.call('codex.autocontinue.set', {'enabled': True})
        self.assertTrue(self.s.codex_state['auto'])
        self.s.call('codex.autocontinue.set', {'enabled': False})
        self.assertTrue(self.s.codex_state['auto'])  # turning it off leaves auto switching alone
        self.s.call('codex.autocontinue.set', {'enabled': True})
        self.s.call('codex.auto.set', {'enabled': False})
        self.assertTrue(self.s.codex_state['autocontinue'])  # auto switching goes off on its own

    def test_setting_persists(self):
        self.s.call('codex.autocontinue.set', {'enabled': True})
        again = Service(self.temp.name, Mock(list=Mock(return_value={'A': {}}), active=Mock(return_value='A')),
                        Mock(fetch=Mock(side_effect=lambda label: row(label))), Mock(), Mock(return_value=[]),
                        codex=self.codex, watch_io=self.io)
        self.assertTrue(again.status()['codex']['autocontinue'])
        with self.assertRaises(QuotaError):
            self.s.call('codex.autocontinue.set', {'enabled': 'yes'})


class ContinueNowTests(unittest.TestCase):
    """The page's "送 continue" button: a thread stopped on the limit, continued on the live account."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.codex = Mock()
        self.codex.continue_thread.return_value = {'tmux': 'codex-continue:codex-01e106', 'watched': True}
        self.codex.app_running.return_value = False
        self.io = Mock()
        self.io.stopped_threads.return_value = [{'thread': T1, 'model': 'gpt-6-astra', 'at': 1.0,
                                                 'cwd': '/work/shop-app'}]
        self.s = Service(self.temp.name, Mock(list=Mock(return_value={}), active=Mock(return_value=None)), Mock(),
                         Mock(), Mock(return_value=[]), codex=self.codex, watch_io=self.io)
        self.s.codex_state.update(active='CODEX_B', profiles={'CODEX_B': codex_row(five=60, active=True)})
        self.s.codex_refresh_stopped()

    def wait(self):
        for _ in range(200):
            if not self.s.codex_manual_continuing:
                return
            time.sleep(0.01)

    def test_stopped_thread_is_listed_and_continued_on_the_live_account(self):
        self.assertEqual([t['project'] for t in self.s.status()['codex']['stopped']], ['shop-app'])
        self.s.call('codex.continue.now', {'thread': T1})
        self.wait()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')
        self.assertEqual(self.s.events[-1]['kind'], 'codex_manualcontinue')
        self.assertEqual(self.s.events[-1]['selected'], 'CODEX_B')
        self.assertEqual(self.s.codex_state['stopped'], [])

    def test_while_the_continued_thread_works_the_text_goes_into_its_cli(self):
        self.s.codex_watches = [{'thread': T1, 'project': 'shop-app', 'tmux': 'codex-continue:codex-01e106',
                                 'status': 'running', 'started_at': stamp()}]
        self.io.stopped_threads.return_value = [{'thread': 'other', 'model': None, 'at': 9.0, 'cwd': '/work/notes'}]
        self.s.codex_refresh_stopped()
        self.s.call('codex.continue.now', {'text': '再check埋'})
        self.io.type.assert_called_once_with('codex-continue:codex-01e106', '再check埋')
        self.codex.continue_thread.assert_not_called()  # not another stopped thread
        self.assertTrue(self.s.events[-1]['queued'])

    def test_once_it_hits_the_limit_again_the_latest_stopped_one_is_picked(self):
        self.s.codex_watches = [{'thread': 'old', 'project': 'x', 'tmux': 't', 'status': 'limited', 'started_at': stamp()}]
        self.s.call('codex.continue.now', {})
        self.wait()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')

    def test_a_thread_stopped_on_another_error_is_reported_once(self):
        now = time.time()
        self.io.recent_threads.return_value = [{'thread': T1, 'cwd': '/work/shop-app', 'last': 'task_error',
                                                'at': now - 60, 'name': '修 bug',
                                                'error': 'Selected model is at capacity. Please try a different model.'}]
        self.s.relinker = Mock(sessions=Mock(return_value=[]))
        self.s.check_session_errors(now)
        self.s.check_session_errors(now + 30)
        errors = [e for e in self.s.events if e['kind'] == 'codex_session_error']
        self.assertEqual([(e['project'], e['error']) for e in errors],
                         [('修 bug', 'Selected model is at capacity. Please try a different model.')])
        again = Service(self.temp.name, Mock(list=Mock(return_value={}), active=Mock(return_value=None)), Mock(),
                        Mock(), Mock(return_value=[]), codex=self.codex, watch_io=self.io)
        again.relinker = self.s.relinker
        again.check_session_errors(now + 60)  # after a restart: not again
        self.assertNotIn('codex_session_error', [e['kind'] for e in again.events[len(self.s.events):]])

    def error_stop(self, at, stop_at=None):
        self.io.recent_threads.return_value = [{'thread': T1, 'cwd': '/work/shop-app', 'last': 'task_error',
                                                'at': stop_at or at, 'name': '修 bug', 'error': 'Selected model is at capacity.'}]
        self.s.relinker = Mock(sessions=Mock(return_value=[]))

    def test_an_error_stop_gets_continue_five_minutes_later_once(self):
        now = time.time()
        self.error_stop(now, now - 60)
        self.s.check_session_errors(now)
        self.codex.continue_thread.assert_not_called()  # not yet
        self.s.check_session_errors(now + 250)
        self.wait()
        self.assertEqual(self.codex.continue_thread.call_args[0][0], T1)  # the same thread, on the live account
        self.assertIn('codex_errorretry', [e['kind'] for e in self.s.events])
        self.s.check_session_errors(now + 400)
        self.wait()
        self.codex.continue_thread.assert_called_once()  # the same stop: not again

    def test_nothing_is_sent_while_the_live_account_is_used_up(self):
        now = time.time()
        self.error_stop(now - 600)
        self.s.codex_state['profiles']['CODEX_B'] = codex_row(five=0, active=True)
        self.s.check_session_errors(now)
        self.wait()
        self.codex.continue_thread.assert_not_called()

    def test_gives_up_after_three_stops_in_a_row(self):
        now = time.time()
        for i in range(4):
            self.error_stop(now, now - 600 + i)  # each retry ended on the error again: a new stop
            self.s.check_session_errors(now + i)
            self.wait()
        self.assertEqual(self.codex.continue_thread.call_count, 3)
        self.assertEqual([e['kind'] for e in self.s.events].count('codex_errorretry_gaveup'), 1)

    def test_an_old_error_stop_is_not_news(self):
        now = time.time()
        self.io.recent_threads.return_value = [{'thread': T1, 'cwd': '/w/x', 'last': 'task_error', 'at': now - 7200,
                                                'name': None, 'error': 'x'}]
        self.s.relinker = Mock(sessions=Mock(return_value=[]))
        self.s.check_session_errors(now)
        self.assertNotIn('codex_session_error', [e['kind'] for e in self.s.events])

    def test_picker_marks_an_error_stop(self):
        self.io.recent_threads.return_value = [{'thread': T1, 'cwd': '/w/x', 'last': 'task_error', 'at': 1.0,
                                                'name': None, 'error': 'x'}]
        self.assertEqual(self.s.continue_sessions('codex')['items'][0]['state'], 'error')

    def test_preview_shows_the_latest_output_of_what_would_get_the_prompt(self):
        self.io.last_output.return_value = ('上一輪結果', '2026-10-12T02:00:00Z')
        r = self.s.call('continue.preview', {'provider': 'codex'})
        self.io.last_output.assert_called_once_with(T1)
        self.assertEqual((r['project'], r['text'], r['target']), ('shop-app', '上一輪結果', False))
        self.codex.continue_thread.assert_not_called()  # read only

    def test_not_while_the_live_account_is_used_up(self):
        self.s.codex_state['profiles']['CODEX_B'] = codex_row(five=0, active=True)
        with self.assertRaises(QuotaError) as e:
            self.s.call('codex.continue.now', {'thread': T1})
        self.assertEqual(str(e.exception), 'ACTIVE_EXHAUSTED')
        self.codex.continue_thread.assert_not_called()

    def test_only_the_thread_that_stopped_last_goes(self):
        self.io.stopped_threads.return_value = [
            {'thread': 'older', 'model': None, 'at': 0.5, 'cwd': '/work/other'},
            {'thread': T1, 'model': 'gpt-6-astra', 'at': 1.0, 'cwd': '/work/shop-app'}]
        self.s.codex_refresh_stopped()
        self.s.call('codex.continue.now', {})
        self.wait()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')
        self.assertEqual([t['thread'] for t in self.s.codex_state['stopped']], ['older'])  # still offered

    def test_own_text_is_sent_instead_of_continue(self):
        self.s.call('codex.continue.now', {'text': '  繼續做\n埋落去  '})
        self.wait()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra', prompt='繼續做 埋落去')

    def test_ten_thousand_characters_fit_through_to_the_daemon(self):
        from service import continue_text
        text = '繼' * 10000
        self.assertEqual(continue_text(text), text)
        with self.assertRaises(QuotaError):
            continue_text(text + '續')
        line = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'codex.continue.now', 'params': {'text': text}},
                          ensure_ascii=False).encode()
        self.assertLess(len(line), 65536)  # the daemon's request line limit
        self.assertLess(len(json.dumps({'method': 'codex.continue.now', 'params': {'text': text}},
                                       ensure_ascii=False).encode()), 65536)  # the web server's body limit

    def test_empty_text_is_refused(self):
        with self.assertRaises(QuotaError) as e:
            self.s.call('codex.continue.now', {'text': '   '})
        self.assertEqual(str(e.exception), 'INVALID_TEXT')

    def test_only_a_known_recent_thread(self):
        self.io.recent_threads.return_value = []
        with self.assertRaises(QuotaError) as e:
            self.s.call('codex.continue.now', {'session': '../../x'})
        self.assertEqual(str(e.exception), 'SESSION_GONE')
        self.codex.continue_thread.assert_not_called()

    def recent(self):
        self.io.recent_threads.return_value = [
            {'thread': T1, 'cwd': '/work/shop-app', 'last': 'task_failed', 'at': 5.0},
            {'thread': 'app', 'cwd': '/work/notes', 'last': 'task_started', 'at': 9.0},
            {'thread': 'idle1', 'cwd': '/work/slot', 'last': 'task_complete', 'at': 7.0}]

    def test_picker_lists_recent_threads_and_defaults_to_the_stopped_one(self):
        self.recent()
        r = self.s.call('continue.sessions', {'provider': 'codex'})
        self.assertEqual([(i['project'], i['state']) for i in r['items']],
                         [('notes', 'busy'), ('slot', 'idle'), ('shop-app', 'stopped')])
        self.assertEqual(r['default'], T1)

    def test_a_picked_idle_thread_is_resumed_with_the_text(self):
        self.recent()
        self.s.call('codex.continue.now', {'session': 'idle1', 'text': '睇吓 log'})
        self.wait()
        self.codex.continue_thread.assert_called_once_with('idle1', None, prompt='睇吓 log')

    def test_a_thread_working_elsewhere_is_refused(self):
        self.recent()
        with self.assertRaises(QuotaError) as e:
            self.s.call('codex.continue.now', {'session': 'app', 'text': 'x'})
        self.assertEqual(str(e.exception), 'THREAD_BUSY')
        self.codex.continue_thread.assert_not_called()

    def test_it_also_leaves_the_waiting_list(self):
        self.s.codex_waiting = [{'thread': T1, 'size': 5}]
        self.s.call('codex.continue.now', {'thread': T1})
        self.wait()
        self.assertEqual(self.s.codex_waiting, [])


if __name__ == '__main__':
    unittest.main()


class WaitForQuotaTests(AutoContinueTests.__base__):
    """Every Codex account used up: what stopped is noted and continued once quota is back."""
    def setUp(self):
        AutoContinueTests.setUp(self)
        self.rows['CODEX_B'] = codex_row(five=0, weekly=40)  # nothing left anywhere
        self.io.rollout.return_value = '/r/rollout.jsonl'
        self.io.size.return_value = 1000
        self.io.turn.return_value = (None, None)  # nothing new in the thread since it was noted

    def test_threads_stopped_while_all_used_up_continue_when_the_live_account_refills(self):
        self.s.refresh_codex()
        self.codex.use.assert_not_called()
        self.codex.continue_thread.assert_not_called()
        self.assertEqual([w['thread'] for w in self.s.codex_waiting], [T1])
        self.rows['CODEX_A'] = codex_row(five=80, active=True)  # 5 h window refilled
        self.s.refresh_codex()
        self.codex.use.assert_not_called()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')
        self.assertEqual(self.s.codex_waiting, [])

    def test_or_after_switching_to_the_account_that_refilled_first(self):
        self.s.refresh_codex()
        self.io.interrupted_threads.return_value = []  # long after the stop: no longer looks interrupted
        self.rows['CODEX_B'] = codex_row(five=60, weekly=40)
        self.s.last_codex_switch = 0
        self.s.refresh_codex()
        self.codex.use.assert_called_once_with('CODEX_B')
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')

    def test_a_thread_continued_by_hand_meanwhile_is_left_alone(self):
        self.s.refresh_codex()
        self.io.size.return_value = 5000  # it grew: someone continued it
        self.io.turn.return_value = ('task_complete', 'DONE')
        self.rows['CODEX_A'] = codex_row(five=80, active=True)
        self.s.refresh_codex()
        self.codex.continue_thread.assert_not_called()

    def test_a_thread_noted_while_its_stopped_turn_was_still_being_written_still_continues(self):
        self.s.refresh_codex()
        self.io.size.return_value = 5000  # only the usage-limit end of the same turn was written after it was noted
        self.io.turn.return_value = ('task_failed', None)
        self.rows['CODEX_A'] = codex_row(five=80, active=True)
        self.s.refresh_codex()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')

    def test_codex_is_looked_at_right_after_the_live_window_resets(self):
        self.s.refresh_codex()
        reset = reset_epoch(self.rows['CODEX_A']['5h']['reset_at'])
        self.assertAlmostEqual(self.s.codex_refill_in(reset - 100), 110)
        self.assertIsNone(self.s.codex_refill_in(reset + 20))  # past it: the normal poll keeps looking

    def test_all_used_up_is_not_reported_again_after_a_restart(self):
        self.s.refresh_codex()
        again = Service(self.temp.name, Mock(list=Mock(return_value={}), active=Mock(return_value=None)), Mock(),
                        Mock(), Mock(return_value=[]), codex=self.codex, watch_io=self.io)
        again.codex_state.update(profiles=self.rows, active='CODEX_A', waitcontinue=True)
        again.codex_auto(time.time())
        self.assertNotIn('codex_all_exhausted', [e['kind'] for e in again.events[len(self.s.events):]])

    def test_one_line_per_continue(self):
        self.s.event('codex_continue_started', project='room')  # logged by the continue script
        self.s.autocontinue_event('room', 'CODEX_A')
        self.assertEqual([e['kind'] for e in self.s.events[-2:]][-1], 'codex_autocontinue')
        self.assertNotIn('codex_continue_started', [e['kind'] for e in self.s.events[-1:]] + [self.s.events[-2]['kind']])

    def test_chatgpt_opening_the_thread_does_not_count_as_continued(self):
        self.s.refresh_codex()
        self.io.size.return_value = 3000  # thread_settings_applied written by ChatGPT, no new turn
        self.rows['CODEX_A'] = codex_row(five=80, active=True)
        self.s.refresh_codex()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')

    def test_manual_switch_hands_the_waiting_threads_over(self):
        self.s.refresh_codex()
        self.assertEqual([w['thread'] for w in self.s.codex_waiting], [T1])
        self.rows['CODEX_B'] = codex_row(five=60, weekly=40)
        self.s.call('codex.use', {'label': 'CODEX_B'})
        self.rows['CODEX_A'], self.rows['CODEX_B'] = codex_row(five=0), codex_row(five=60, weekly=40, active=True)
        self.s.last_codex_switch = 0
        self.s.refresh_codex()
        self.codex.continue_thread.assert_not_called()
        self.assertEqual(self.s.codex_waiting, [])
        self.assertIn('codex_waiting_cleared', [e['kind'] for e in self.s.events])

    def test_waiting_list_survives_a_restart(self):
        self.s.refresh_codex()
        again = Service(self.temp.name, Mock(list=Mock(return_value={}), active=Mock(return_value=None)), Mock(),
                        Mock(), Mock(return_value=[]), codex=self.codex, watch_io=self.io)
        self.assertEqual([w['thread'] for w in again.codex_waiting], [T1])

    def test_wait_off_notes_nothing(self):
        self.s.call('codex.waitcontinue.set', {'enabled': False})
        self.s.refresh_codex()
        self.assertEqual(self.s.codex_waiting, [])

    def test_waiting_works_with_auto_switch_off(self):
        self.s.call('codex.auto.set', {'enabled': False})
        self.rows['CODEX_B'] = codex_row(five=60, weekly=40)  # another account has quota, but no auto switch
        self.s.refresh_codex()
        self.codex.use.assert_not_called()
        self.assertEqual([w['thread'] for w in self.s.codex_waiting], [T1])
        self.assertIn('codex_waiting_quota', [e['kind'] for e in self.s.events])
        self.rows['CODEX_A'] = codex_row(five=80, active=True)
        self.s.refresh_codex()
        self.codex.use.assert_not_called()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')

    def test_the_two_settings_are_independent(self):
        self.s.call('codex.autocontinue.set', {'enabled': False})  # no continue right after a switch ...
        self.s.refresh_codex()
        self.rows['CODEX_A'] = codex_row(five=80, active=True)
        self.s.refresh_codex()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')  # ... still waits for quota

    def test_switch_continues_only_with_its_own_setting(self):
        self.s.call('codex.waitcontinue.set', {'enabled': False})
        self.rows['CODEX_B'] = codex_row(five=60, weekly=40)
        self.s.call('codex.autocontinue.set', {'enabled': True})
        self.s.refresh_codex()
        self.codex.continue_thread.assert_called_once_with(T1, 'gpt-6-astra')
        self.assertEqual(self.s.codex_waiting, [])
