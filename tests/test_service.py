import copy
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from quota import QuotaError, QuotaClient, normalize
import isolation  # noqa: F401  (no real Codex/agy helpers in tests)
from service import DEFAULTS, Service, metrics, validate
from broker import Broker, etime_seconds, running, working


def row(label, five=80, weekly=70, age=0):
    return {'label': label, 'status': 'OK',
            'updated_at': datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat(),
            'groups': [{'family': 'gemini', 'windows': {
                '5h': {'remaining_percent': five, 'reset_at': None},
                'weekly': {'remaining_percent': weekly, 'reset_at': None}}}]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profiles = Mock()
        self.profiles.list.return_value = {'A': {'id': 'a'}, 'B': {'id': 'b'}, 'C': {'id': 'c'}}
        self.profiles.active.return_value = 'A'
        self.client = Mock()
        self.client.fetch.side_effect = lambda label: row(label)
        self.broker = Mock()
        self.broker.switch.side_effect = lambda label, **kw: {'selected': label}
        self.busy = Mock(return_value=[])
        self.s = Service(self.temp.name, self.profiles, self.client, self.broker, self.busy)
        self.s.active = 'A'
        self.s.rows = {'A': row('A', 1), 'B': row('B'), 'C': row('C', 50, 60)}

    def test_pause_persists_and_keeps_quota_polling(self):
        self.s.set_enabled(True)
        self.s.call('pause', {'seconds': 1800})
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.s.refresh()
        self.assertEqual(self.client.fetch.call_count, 3)
        restarted = Service(self.temp.name, self.profiles, self.client, self.broker, self.busy)
        self.assertGreater(restarted.paused_until, time.time())
        self.s.call('resume', {})
        self.s.rows['A'] = row('A', 1)
        self.s.tick()
        self.broker.switch.assert_called_once()

    def test_off_works_when_profile_metadata_is_unavailable(self):
        self.s.set_enabled(True)
        self.profiles.list.side_effect = QuotaError('PROFILES_UNAVAILABLE')
        self.s.set_enabled(False)
        self.assertFalse(self.s.config['enabled'])
        self.assertIsNone(self.s.pending)

    def test_restart_with_unfinished_switch_blocks_rotation(self):
        self.s.set_enabled(True)
        self.s.transaction = {'phase': 'STARTED', 'selected': 'B'}
        self.s.persist()
        restarted = Service(self.temp.name, self.profiles, self.client, self.broker, self.busy)
        restarted.rows = self.s.rows
        restarted.active = 'A'
        self.assertTrue(restarted.status()['recovery_required'])
        restarted.tick()
        self.broker.switch.assert_not_called()
        restarted.call('recovery.ack', {})
        self.assertFalse(restarted.recovery_required)
        self.assertEqual(restarted.transaction['phase'], 'ACKNOWLEDGED')

    def test_uncertain_manual_switch_requires_recovery(self):
        self.broker.switch.side_effect = QuotaError('SWITCH_TIMEOUT_CHECK_CURRENT')
        with self.assertRaises(QuotaError):
            self.s.manual_use({'label':'B'})
        self.assertTrue(self.s.recovery_required)
        self.assertEqual(self.s.transaction['phase'], 'UNKNOWN')
        self.assertFalse(self.s.switching)

    def test_switch_refused_before_keychain_change_needs_no_recovery(self):
        self.broker.switch.side_effect = QuotaError('CLOSE_TIMEOUT_NOT_SWITCHED')
        with self.assertRaises(QuotaError):
            self.s.manual_use({'label':'B'})
        self.assertFalse(self.s.recovery_required)
        self.assertEqual(self.s.transaction['phase'], 'FAILED')

    def test_all_accounts_visible_before_first_quota_response(self):
        fresh = Service(self.temp.name, self.profiles, self.client, self.broker, self.busy)
        self.assertEqual(set(fresh.status()['profiles']), {'A', 'B', 'C'})
        self.assertTrue(all(r['status'] == 'UNAVAILABLE' for r in fresh.rows.values()))

    def test_corrupt_state_requires_recovery_before_rotation(self):
        self.s.set_enabled(True)
        (Path(self.temp.name) / 'state.json').write_text('{broken')
        restarted = Service(self.temp.name, self.profiles, self.client, self.broker, self.busy)
        restarted.rows = self.s.rows
        restarted.active = 'A'
        self.assertTrue(restarted.recovery_required)
        restarted.tick()
        self.broker.switch.assert_not_called()

    def test_default_off_still_refreshes_all_profiles(self):
        self.assertFalse(self.s.config['enabled'])
        self.s.refresh()
        self.assertEqual(self.client.fetch.call_count, 3)
        self.s.tick()
        self.broker.switch.assert_not_called()

    def test_running_poll_loop_continues_while_off(self):
        thread = threading.Thread(target=self.s.poll_loop)
        thread.start()
        try:
            deadline = time.time()+2
            while self.client.fetch.call_count < 3 and time.time() < deadline:
                time.sleep(.01)
            self.s.set_enabled(False)
            self.s.refresh_requested.set()
            deadline = time.time()+2
            while self.client.fetch.call_count < 6 and time.time() < deadline:
                time.sleep(.01)
            self.assertGreaterEqual(self.client.fetch.call_count, 6)
            self.broker.switch.assert_not_called()
        finally:
            self.s.stop.set(); self.s.refresh_requested.set(); thread.join(2)

    def test_off_cancels_pending_without_erasing_quota(self):
        self.s.set_enabled(True)
        self.busy.return_value = [123]
        self.s.tick()
        self.assertEqual(self.s.pending['state'], 'WAITING_FOR_IDLE')
        self.s.set_enabled(False)
        self.assertIsNone(self.s.pending)
        self.assertEqual(len(self.s.rows), 3)
        self.busy.return_value = []
        self.s.tick()
        self.broker.switch.assert_not_called()

    def test_low_quota_chooses_best_ready_target_when_idle(self):
        self.s.set_enabled(True)
        self.s.tick()
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_failed_and_stale_data_never_drive_a_switch(self):
        self.s.set_enabled(True)
        self.s.rows['B']['status'] = 'STALE'
        self.s.rows['C'] = row('C', age=10000)
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertEqual(self.s.pending['state'], 'NO_READY_PROFILE')

    def test_unknown_current_quota_does_not_trigger(self):
        self.s.set_enabled(True)
        self.s.rows['A']['groups'][0]['windows']['5h']['remaining_percent'] = None
        self.s.tick()
        self.broker.switch.assert_not_called()

    def test_boundary_is_strictly_below_threshold(self):
        self.s.set_enabled(True)
        self.s.rows['A'] = row('A', five=5)
        self.s.tick()
        self.broker.switch.assert_not_called()

    def test_refresh_failure_marks_old_values_stale(self):
        self.client.fetch.side_effect = QuotaError('QUOTA_HTTP_403')
        self.s.refresh()
        self.assertEqual(self.s.rows['A']['status'], 'STALE')
        self.assertIsNone(metrics(self.s.rows['A'], self.s.config, time.time()))

    def test_one_transient_failure_keeps_fresh_values(self):
        # A single network / 429 / 5xx blip must not flash 資料待更新 on the live account.
        for error in ('NETWORK_UNAVAILABLE', 'QUOTA_HTTP_429', 'QUOTA_HTTP_503'):
            self.s.rows['A'] = row('A', five=80)
            self.client.fetch.side_effect = QuotaError(error)
            self.s.refresh()
            self.assertEqual(self.s.rows['A']['status'], 'OK', error)
            self.assertEqual(self.s.rows['A']['last_error'], error)
            self.assertIsNotNone(metrics(self.s.rows['A'], self.s.config, time.time()))

    def test_transient_failures_still_go_stale_once_values_are_old(self):
        self.s.rows['A'] = row('A', five=80, age=300)
        self.client.fetch.side_effect = QuotaError('NETWORK_UNAVAILABLE')
        self.s.refresh()
        self.assertEqual(self.s.rows['A']['status'], 'STALE')

    def test_internal_exception_does_not_escape_into_response(self):
        self.client.fetch.side_effect = RuntimeError('fake-sensitive-value')
        self.s.refresh()
        self.assertNotIn('fake-sensitive-value', json.dumps(self.s.status()))

    def test_config_conflict_is_atomic(self):
        revision = self.s.revision
        self.s.set_enabled(True)
        with self.assertRaisesRegex(QuotaError, 'CONFIG_CONFLICT'):
            self.s.configure({'revision': revision, 'config': DEFAULTS})
        self.assertTrue(json.loads(self.s.config_path.read_text())['enabled'])

    def test_invalid_config_does_not_write(self):
        before = self.s.config_path.read_bytes()
        invalid = {**self.s.config, 'hourly_minutes': [61]}
        with self.assertRaises(QuotaError):
            self.s.configure({'revision': self.s.revision, 'config': invalid})
        self.assertEqual(before, self.s.config_path.read_bytes())

    def test_drift_cancels_automatic_switch(self):
        self.s.set_enabled(True)
        self.profiles.active.return_value = 'C'
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertIsNone(self.s.pending)

    def test_manual_switch_allowed_when_master_off(self):
        self.s.manual_use({'label': 'B', 'now': True})
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_manual_wait_idle_switches_only_after_agy_finishes(self):
        self.busy.return_value = [123]
        self.s.manual_use({'label': 'C', 'wait_idle': True})
        self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertEqual(self.s.pending['state'], 'WAITING_FOR_IDLE')
        self.assertEqual(self.s.pending['target'], 'C')
        self.busy.return_value = []
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)
        self.assertEqual(self.s.active, 'C')
        self.assertIsNone(self.s.pending)

    def test_manual_wait_idle_never_forces_even_when_quota_exhausted(self):
        self.s.set_enabled(True)
        self.s.rows['A'] = row('A', five=0)
        self.busy.return_value = [123]
        self.s.manual_use({'label': 'C', 'wait_idle': True})
        for step in range(5):
            self.s.tick(time.time() + step * 120)
        self.broker.switch.assert_not_called()
        self.assertEqual(self.s.pending['target'], 'C')

    def test_manual_wait_idle_survives_master_off_and_config_save(self):
        self.busy.return_value = [123]
        self.s.manual_use({'label': 'C', 'wait_idle': True})
        self.s.set_enabled(True)
        self.s.set_enabled(False)
        self.busy.return_value = []
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_cancel_manual_wait_does_not_block_automation(self):
        self.busy.return_value = [123]
        self.s.manual_use({'label': 'C', 'wait_idle': True})
        self.s.call('cancel', {})
        self.assertIsNone(self.s.pending)
        self.assertEqual(self.s.cancelled_until, 0)

    def test_manual_now_closes_even_when_auto_close_disabled(self):
        self.s.config['close_app'] = False
        self.s.manual_use({'label': 'B', 'now': True})
        self.broker.switch.assert_called_once_with('B', close_all=True)

    def test_cooldown_prevents_immediate_repeat(self):
        self.s.set_enabled(True)
        self.s.tick()
        self.s.rows['B'] = row('B', five=0)
        self.s.tick()
        self.assertEqual(self.broker.switch.call_count, 1)

    def test_time_rule_independent_of_usage_rule(self):
        self.s.config.update(enabled=True, usage_enabled=False, time_enabled=True,
                             hourly_minutes=[datetime.now().minute])
        self.s.rows['A'] = row('A')
        self.s.tick()
        self.broker.switch.assert_called_once()

    def test_notify_only_never_switches_and_deduplicates(self):
        self.s.set_enabled(True)
        self.s.config['mode'] = 'notify'
        self.s.tick(); self.s.tick()
        self.broker.switch.assert_not_called()
        self.assertEqual(self.s.pending['state'], 'NOTIFY_ONLY')
        self.assertEqual(sum(e['kind']=='rotation_recommended' for e in self.s.events), 1)

    def test_exhausted_waits_countdown_then_uses_original_close_all(self):
        self.s.set_enabled(True)
        self.s.rows['A'] = row('A', five=0)
        self.busy.return_value = [123]
        now = time.time()
        self.s.tick(now)
        self.assertEqual(self.s.pending['state'], 'COUNTDOWN')
        self.broker.switch.assert_not_called()
        self.s.tick(now + 61)
        self.broker.switch.assert_called_once_with('B', close_all=True, force=True)  # used up: switch past open agy

    def test_master_off_cancels_countdown_but_refresh_remains_available(self):
        self.s.set_enabled(True)
        self.s.rows['A'] = row('A', five=0)
        self.busy.return_value = [123]
        now = time.time()
        self.s.tick(now)
        self.s.set_enabled(False)
        self.s.tick(now + 61)
        self.broker.switch.assert_not_called()
        self.s.refresh()
        self.assertEqual(self.client.fetch.call_count, 3)

    def test_cancel_suppresses_immediate_retrigger(self):
        self.s.set_enabled(True)
        self.s.rows['A'] = row('A', five=0)
        self.busy.return_value = [123]
        now = time.time()
        self.s.tick(now)
        self.s.call('cancel', {})
        self.s.tick(now + 61)
        self.assertIsNone(self.s.pending)
        self.broker.switch.assert_not_called()

    def test_close_app_disabled_never_interrupts(self):
        self.s.set_enabled(True)
        self.s.config['close_app'] = False
        self.s.rows['A'] = row('A', five=0)
        self.busy.return_value = [123]
        self.s.tick()
        self.assertEqual(self.s.pending['state'], 'WAITING_FOR_IDLE')
        self.broker.switch.assert_not_called()

    def test_unselected_window_does_not_trigger(self):
        self.s.set_enabled(True)
        self.s.config['windows'] = ['weekly']
        self.s.tick()
        self.broker.switch.assert_not_called()

    def test_nonparticipant_not_selected(self):
        self.s.set_enabled(True)
        self.s.config['participants'] = ['A', 'C']
        self.s.tick()
        self.broker.switch.assert_called_once_with('C', close_all=True)

    def test_empty_or_unknown_participants_rejected(self):
        for participants in ([], ['unknown']):
            with self.assertRaises(QuotaError):
                self.s.configure({'revision': self.s.revision,
                                  'config': {**self.s.config, 'participants': participants}})

    def test_off_responds_while_already_started_switch_finishes(self):
        self.s.set_enabled(True)
        entered, release = threading.Event(), threading.Event()
        def slow_switch(*args, **kwargs):
            entered.set(); release.wait(2)
            return {'selected': 'B'}
        self.broker.switch.side_effect = slow_switch
        thread = threading.Thread(target=self.s.tick)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.s.set_enabled(False)
            self.assertTrue(self.s.status()['switching'])
            self.assertFalse(self.s.config['enabled'])
        finally:
            release.set(); thread.join(2)

    def test_lost_target_cancels_visible_countdown(self):
        self.s.set_enabled(True)
        self.s.rows['A'] = row('A', five=0)
        self.busy.return_value = [123]
        self.s.tick()
        self.assertIn('deadline', self.s.pending)
        self.s.rows['B']['status'] = 'STALE'
        self.s.rows['C']['status'] = 'STALE'
        self.s.tick()
        self.assertNotIn('deadline', self.s.pending)
        self.broker.switch.assert_not_called()

    def test_zero_is_distinct_from_unknown(self):
        self.s.set_enabled(True)
        self.s.rows = {k: row(k, five=0) for k in 'ABC'}
        self.s.tick()
        self.assertEqual(self.s.pending['state'], 'ALL_EXHAUSTED')
        self.s.rows['C']['status'] = 'UNAVAILABLE'
        self.s.tick()
        self.assertEqual(self.s.pending['state'], 'NO_READY_PROFILE')

    def test_history_survives_service_restart(self):
        self.s.set_enabled(False)
        other = Service(self.temp.name, self.profiles, self.client, self.broker, self.busy)
        self.assertEqual(other.events[-1]['kind'], 'config_changed')
        self.assertNotEqual(other.revision, self.s.revision)


class QuotaTests(unittest.TestCase):
    def test_missing_fraction_is_unknown_and_families_stay_separate(self):
        data = {'groups': [
            {'displayName': 'Gemini Models', 'buckets': [{'window': '5h'}, {'window': 'weekly', 'remainingFraction': .5}]},
            {'displayName': 'Claude and GPT models', 'buckets': [{'window': '5h', 'remainingFraction': 1}]}]}
        result = normalize(data)
        self.assertIsNone(result[0]['windows']['5h']['remaining_percent'])
        self.assertEqual(result[1]['windows']['5h']['remaining_percent'], 100)

    def test_nan_or_out_of_range_is_not_ready(self):
        for bad in (float('nan'), -1, 2, True, '0.9'):
            with self.assertRaises(QuotaError):
                normalize({'groups': [{'displayName': 'Gemini', 'buckets': [{'window': '5h', 'remainingFraction': bad}]}]})

    def test_ambiguous_windows_rejected(self):
        with self.assertRaises(QuotaError):
            normalize({'groups': [{'displayName': 'Gemini', 'buckets': [
                {'window': '5h', 'remainingFraction': .5}, {'window': '5h', 'remainingFraction': .2}]}]})

    def test_refresh_cached_in_memory_without_keychain_write(self):
        client = QuotaClient(profiles=Mock())
        token = {'refresh_token': 'test-refresh'}
        with patch.object(client, 'oauth_client', return_value=('test-client', 'test-secret')), \
             patch('quota.request', return_value={'access_token': 'test-access', 'expires_in': 3600}) as request:
            self.assertEqual(client.access_token(token, 'test-audience', 'test-id'), 'test-access')
            self.assertEqual(client.access_token(token, 'test-audience', 'test-id'), 'test-access')
            request.assert_called_once()

    def test_unsupported_client_fails_closed(self):
        with self.assertRaisesRegex(QuotaError, 'OAUTH_CLIENT_UNSUPPORTED'):
            QuotaClient().oauth_client('unknown')


class BrokerTests(unittest.TestCase):
    def test_delegates_to_original_manual_command(self):
        profiles = Mock()
        profiles.list.return_value = {'A': {}, 'B': {}}
        profiles.active.side_effect = ['A', 'B']
        broker = Broker('/unused', profiles, Path('/helper/agy-account'))
        result = Mock(returncode=0, stdout=b'{"activation":"OK","selected":"B"}')
        with patch('broker.subprocess.run', return_value=result) as run:
            broker.switch('B', close_all=True)
            self.assertEqual(run.call_args.args[0], ['/helper/agy-account', '--json', 'use', 'B', '--close-all'])

    def test_helper_refusal_reason_is_kept_as_error_code(self):
        profiles = Mock()
        profiles.list.return_value = {'A': {}, 'B': {}}
        profiles.active.return_value = 'A'
        broker = Broker('/unused', profiles, Path('/helper/agy-account'))
        for stderr, code in [(b'ERROR: could not stop [123] within 30s; live state untouched', 'CLOSE_TIMEOUT_NOT_SWITCHED'),
                             (b'ERROR: agy is running (pids [5]); a running agy may ...', 'AGY_RUNNING_NOT_SWITCHED'),
                             (b'ERROR: another account switch is in progress', 'SWITCH_BUSY'),
                             (b'Traceback ...', 'SWITCH_FAILED_CHECK_CURRENT')]:
            with patch('broker.subprocess.run', return_value=Mock(returncode=1, stdout=b'', stderr=stderr)):
                with self.assertRaises(QuotaError) as e:
                    broker.switch('B', close_all=True)
                self.assertEqual(str(e.exception), code)

    def test_orphaned_scheduler_alone_is_idle(self):
        cmds = {901: '/Applications/Antigravity.app/Contents/Resources/bin/language_server multicall schedule 0 */5 * * *'}
        def pgrep(args, **kw):
            pids = [p for p, c in cmds.items() if args[1] == '-f' and re.search(args[2], c)]
            return Mock(stdout=''.join(f'{p}\n' for p in pids).encode())
        with patch('broker.subprocess.run', side_effect=pgrep):
            self.assertEqual(running(), [])
        cmds[10] = '/Applications/Antigravity.app/Contents/MacOS/Antigravity'
        with patch('broker.subprocess.run', side_effect=pgrep):
            self.assertEqual(running(), [10])


class WorkingTests(unittest.TestCase):
    APP = '/Applications/Antigravity.app/Contents/Resources/bin/language_server'

    def check(self, rows, now=1000.0, mtimes=()):
        with tempfile.TemporaryDirectory() as d:
            for i, m in enumerate(mtimes):
                f = Path(d) / f'{i}.db'
                f.write_text('')
                os.utime(f, (m, m))
            return working(rows, now, [d])

    def test_open_but_quiet_app_and_interactive_agy_are_idle(self):
        rows = [('10', '1', '30:00', self.APP + ' --standalone'),
                ('11', '10', '29:45', self.APP + ' multicall schedule 0 */5 * * * /x/job'),
                ('20', '1', '10:00', '/opt/homebrew/bin/agy --dangerously-skip-permissions'),
                ('21', '20', '09:58', 'node mcp-server.js')]
        self.assertEqual(self.check(rows, mtimes=[900]), [])

    def test_print_mode_agy_is_work(self):
        rows = [('20', '1', '00:05', 'agy -p Return exactly: OK --print-timeout 90s')]
        self.assertEqual(self.check(rows), ['agy_print:20'])

    def test_prompt_interactive_is_not_print_mode(self):
        self.assertEqual(self.check([('20', '1', '00:05', 'agy --prompt-interactive hello')]), [])

    def test_tool_command_started_after_agy_is_work(self):
        rows = [('20', '1', '05:00', '/opt/homebrew/bin/agy'), ('30', '20', '00:40', 'sleep 40')]
        self.assertEqual(self.check(rows), ['child:30'])

    def test_finished_child_left_as_zombie_is_idle(self):
        rows = [('20', '1', '05:00', '/opt/homebrew/bin/agy'), ('30', '20', '00:40', '<defunct>')]
        self.assertEqual(self.check(rows), [])

    def test_scheduled_job_child_is_work(self):
        rows = [('11', '1', '2-01:00:00', self.APP + ' multicall schedule 0 */5 * * * /x/job'),
                ('40', '11', '00:10', '/x/job new-conversation')]
        self.assertEqual(self.check(rows), ['child:40'])

    def test_recent_conversation_write_is_work(self):
        self.assertEqual(self.check([], now=1000.0, mtimes=[970]), ['conversation_write'])

    def test_conversation_write_by_the_rotators_own_agy_is_not_work(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'warm.db'
            f.write_text('')
            os.utime(f, (970, 970))
            self.assertEqual(working([], 1000.0, [d], ignore_until=975), [])
            os.utime(f, (990, 990))  # written after the rotator's run ended: someone else
            self.assertEqual(working([], 1000.0, [d], ignore_until=975), ['conversation_write'])

    def test_service_marks_the_end_of_its_own_agy_runs(self):
        import types
        s = types.SimpleNamespace(own_agy_until=0.0, broker=Mock())
        s.own_agy = lambda fn, *a, **k: Service.own_agy(s, fn, *a, **k)
        before = time.time()
        s.own_agy(s.broker.warm, 'GEMINI_A')
        self.assertGreaterEqual(s.own_agy_until, before)
        s.broker.switch.side_effect = QuotaError('X')
        mark = s.own_agy_until
        with self.assertRaises(QuotaError):
            s.own_agy(s.broker.switch, 'GEMINI_B')
        self.assertGreater(s.own_agy_until, mark)  # failed runs wrote too

    def test_etime_formats(self):
        self.assertEqual([etime_seconds(v) for v in ('00:05', '01:02:03', '2-00:00:10')], [5, 3723, 172810])


if __name__ == '__main__':
    unittest.main()
