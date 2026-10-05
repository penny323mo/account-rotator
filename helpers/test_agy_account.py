"""No live Keychain/process operations: verify the existing command's contract."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('account', str(Path(__file__).with_name('agy-account')))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


class SwitchFixture(unittest.TestCase):
    def setUp(self):
        self.store = {(m.LIVE_SVC, m.LIVE_ACCT): 'B:new', **{
            (m.STORE_SVC, f'GEMINI_{x}'): f'{x}:old' for x in 'ABC'}}
        self.meta = {'profiles': {f'GEMINI_{x}': {'id': x} for x in 'ABC'}}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(m, 'HOME', temp))
        self.stack.enter_context(patch.object(m, 'load_meta', return_value=self.meta))
        self.stack.enter_context(patch.object(m, 'identity', side_effect=lambda x: x.split(':')[0] if x else None))
        self.stack.enter_context(patch.object(m, 'kc_read', side_effect=lambda s, a: self.store.get((s, a))))
        self.write = self.stack.enter_context(patch.object(m, 'kc_write', side_effect=lambda s, a, v: self.store.__setitem__((s, a), v)))
        self.close = self.stack.enter_context(patch.object(m, 'close_all', return_value=(True, [])))
        self.stack.enter_context(patch.object(m, 'agy_running', return_value=[]))
        self.stack.enter_context(patch.object(m, 'app_running', return_value=False))
        self.procs = self.stack.enter_context(patch.object(m, 'app_procs', return_value=set()))
        self.clock = [0.0]
        self.stack.enter_context(patch.object(m.time, 'time', lambda: self.clock[0]))
        self.stack.enter_context(patch.object(m.time, 'sleep', lambda s: self.clock.__setitem__(0, self.clock[0] + s)))
        self.save = self.stack.enter_context(patch.object(m, 'save_meta'))
        self.run = self.stack.enter_context(patch.object(m.subprocess, 'run'))
        self.run.return_value = types.SimpleNamespace(returncode=0)



class SwitchTests(SwitchFixture):
    def use(self, label, close_all=True, force=False):
        m.cmd_use(types.SimpleNamespace(label=label, close_all=close_all, force=force, json=True))

    def test_idle_switch_refuses_while_orphaned_scheduler_survives(self):
        self.procs.return_value = {901}  # ignores SIGTERM in this fixture
        with self.assertRaises(SystemExit) as error: self.use('GEMINI_A', close_all=False)
        self.assertIn('could not stop', str(error.exception.code))
        self.write.assert_not_called()

    def test_each_original_command_activates_requested_identity(self):
        for label in ['GEMINI_A', 'GEMINI_B', 'GEMINI_C']:
            self.use(label)
            self.assertEqual(m.identity(self.store[(m.LIVE_SVC, m.LIVE_ACCT)]), label[-1])
        self.assertEqual(self.run.call_count, 3)

    def test_invalid_target_does_not_close_app(self):
        del self.store[(m.STORE_SVC, 'GEMINI_A')]
        with self.assertRaises(SystemExit): self.use('GEMINI_A')
        self.close.assert_not_called()
        self.write.assert_not_called()

    def test_close_timeout_reopens_without_switching(self):
        self.close.return_value = (True, [123])
        with self.assertRaises(SystemExit): self.use('GEMINI_A')
        self.write.assert_not_called()
        self.run.assert_called_once_with(['open', '-a', m.APP])

    def test_used_up_account_switches_past_an_agy_that_would_not_stop(self):
        # --force (rotator: the live account is at 0 %): an agy left open no longer blocks the switch.
        self.close.return_value = (True, [123])
        self.stack.enter_context(patch.object(m, 'agy_running', return_value=[123]))
        self.use('GEMINI_A', force=True)
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'A:old')

    def test_metadata_failure_still_reopens(self):
        self.save.side_effect = OSError('fixture')
        with self.assertRaises(OSError): self.use('GEMINI_A')
        self.run.assert_called_once_with(['open', '-a', m.APP])

    def test_same_profile_preserves_new_live_token(self):
        self.use('GEMINI_B')
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'B:new')

    def test_activation_failure_rolls_back(self):
        def write(service, account, value):
            if service == m.LIVE_SVC and value.startswith('A:'):
                raise RuntimeError('fixture')
            self.store[(service, account)] = value
        self.write.side_effect = write
        with self.assertRaises(SystemExit) as error: self.use('GEMINI_A')
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'B:new')
        self.run.assert_called_once_with(['open', '-a', m.APP])

    def test_rollback_failure_is_not_reported_as_rolled_back(self):
        def write(service, account, value):
            if service == m.LIVE_SVC:
                raise RuntimeError('fixture')
            self.store[(service, account)] = value
        self.write.side_effect = write
        with self.assertRaises(SystemExit): self.use('GEMINI_A')
        self.assertEqual(json.loads(self.output.getvalue())['activation'], 'ROLLBACK_FAILED')

    def test_app_reopen_failure_does_not_claim_app_reopened(self):
        self.run.return_value = types.SimpleNamespace(returncode=1)
        self.use('GEMINI_A')
        result = json.loads(self.output.getvalue())
        self.assertFalse(result['app_reopened'])
        self.assertEqual(result['activation'], 'OK')


class WarmTests(SwitchFixture):
    """Warm-up: briefly make an idle account live, send one tiny agy prompt, restore the previous one."""
    def setUp(self):
        super().setUp()
        self.seen_live = []
        def smoke(timeout):
            self.seen_live.append(self.store[(m.LIVE_SVC, m.LIVE_ACCT)])
            return {'status': 'PASS', 'exit': 0, 'elapsed_s': 1.0, 'detail': None}
        self.smoke = self.stack.enter_context(patch.object(m, 'smoke', side_effect=smoke))
        self.cli = self.stack.enter_context(patch.object(m, 'agy_cli_running', return_value=[]))

    def warm(self, label):
        m.cmd_warm(types.SimpleNamespace(label=label, timeout=60, json=True))
        return json.loads(self.output.getvalue())

    def test_runs_prompt_as_target_and_restores_previous(self):
        result = self.warm('GEMINI_A')
        self.assertEqual(self.seen_live, ['A:old'])
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'B:new')
        self.assertEqual((result['warmed'], result['status'], result['restored']), ('GEMINI_A', 'PASS', 'GEMINI_B'))
        self.close.assert_not_called()

    def test_refreshed_target_token_is_kept(self):
        def smoke(timeout):
            self.store[(m.LIVE_SVC, m.LIVE_ACCT)] = 'A:refreshed'
            return {'status': 'PASS', 'exit': 0, 'elapsed_s': 1.0, 'detail': None}
        self.smoke.side_effect = smoke
        self.warm('GEMINI_A')
        self.assertEqual(self.store[(m.STORE_SVC, 'GEMINI_A')], 'A:refreshed')
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'B:new')

    def test_other_account_written_back_meanwhile_is_not_stored_as_target(self):
        def smoke(timeout):
            self.store[(m.LIVE_SVC, m.LIVE_ACCT)] = 'B:newer'  # app persisted its own token
            return {'status': 'PASS', 'exit': 0, 'elapsed_s': 1.0, 'detail': None}
        self.smoke.side_effect = smoke
        self.warm('GEMINI_A')
        self.assertEqual(self.store[(m.STORE_SVC, 'GEMINI_A')], 'A:old')
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'B:newer')

    def test_restores_even_when_prompt_crashes(self):
        self.smoke.side_effect = RuntimeError('fixture')
        with self.assertRaises(RuntimeError): self.warm('GEMINI_A')
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'B:new')

    def test_refuses_while_agy_cli_is_running(self):
        self.cli.return_value = [42]
        with self.assertRaises(SystemExit) as error: self.warm('GEMINI_A')
        self.assertIn('BUSY', str(error.exception.code))
        self.write.assert_not_called()
        self.smoke.assert_not_called()

    def test_live_account_is_warmed_in_place(self):
        result = self.warm('GEMINI_B')
        self.assertEqual(self.seen_live, ['B:new'])
        self.write.assert_not_called()
        self.assertEqual((result['warmed'], result['restored'], result['restore_ok']), ('GEMINI_B', 'GEMINI_B', True))

    def test_refuses_when_live_is_not_enrolled(self):
        self.store[(m.LIVE_SVC, m.LIVE_ACCT)] = 'Z:unknown'
        with self.assertRaises(SystemExit): self.warm('GEMINI_A')
        self.write.assert_not_called()


class FakeProcs:
    """Process table stand-in for pgrep/pkill/osascript; pgrep -f matches like the real tool (regex search)."""
    MAIN = m.APP + '/Contents/MacOS/Antigravity'
    SCHED = m.APP + '/Contents/Resources/bin/language_server multicall schedule 0 */5 * * *'

    def __init__(self, procs, ignores_term=()):
        self.procs, self.ignores_term, self.clock, self.calls = dict(procs), set(ignores_term), 0.0, []

    def match(self, args):
        if args[1] == '-x':
            return [p for p, c in self.procs.items() if c.split('/')[-1].split()[0] == args[2]]
        return [p for p, c in self.procs.items() if re.search(args[2], c)]

    def run(self, args, **kw):
        self.calls.append(args)
        if args[0] == 'osascript':
            self.procs = {p: c for p, c in self.procs.items() if p >= 900}  # app + children quit; orphans (>=900) stay
            return types.SimpleNamespace(returncode=0, stdout='')
        if args[0] == 'pgrep':
            pids = self.match(args)
            return types.SimpleNamespace(returncode=0 if pids else 1, stdout=''.join(f'{p}\n' for p in pids))
        if args[0] == 'pkill':
            for p in self.match(args[1:]):
                if p not in self.ignores_term:
                    del self.procs[p]
            return types.SimpleNamespace(returncode=0, stdout='')
        raise AssertionError(args)

    def sleep(self, s):
        self.clock += s


class OrphanCloseTests(unittest.TestCase):
    def close(self, fake):
        with patch.object(m.subprocess, 'run', side_effect=fake.run), \
             patch.object(m.time, 'time', lambda: fake.clock), patch.object(m.time, 'sleep', fake.sleep):
            return m.close_all(wait=5)

    def test_orphaned_scheduler_from_previous_launch_is_terminated(self):
        fake = FakeProcs({10: FakeProcs.MAIN, 11: FakeProcs.SCHED, 901: FakeProcs.SCHED})
        self.assertEqual(self.close(fake), (True, []))
        self.assertIn(['pkill', '-TERM', '-f', '^' + m.APP + '/'], fake.calls)

    def test_orphans_alone_do_not_count_as_running_app(self):
        fake = FakeProcs({901: FakeProcs.SCHED})
        self.assertEqual(self.close(fake), (False, []))
        self.assertFalse(any(c[0] == 'osascript' for c in fake.calls))

    def test_stop_leftovers_terminates_orphans_without_app(self):
        fake = FakeProcs({901: FakeProcs.SCHED})
        with patch.object(m.subprocess, 'run', side_effect=fake.run), \
             patch.object(m.time, 'time', lambda: fake.clock), patch.object(m.time, 'sleep', fake.sleep):
            self.assertEqual(m.stop_leftovers(), [])
        self.assertFalse(any(c[0] == 'osascript' for c in fake.calls))

    def test_unrelated_process_mentioning_app_path_is_ignored(self):
        fake = FakeProcs({950: "/bin/zsh -c pgrep -f '/Applications/Antigravity.app/Contents/MacOS/Antigravity'"})
        self.assertEqual(self.close(fake), (False, []))
        self.assertFalse(any(c[0] == 'osascript' for c in fake.calls))

    def test_scheduler_ignoring_sigterm_blocks_switch(self):
        fake = FakeProcs({10: FakeProcs.MAIN, 901: FakeProcs.SCHED}, ignores_term={901})
        self.assertEqual(self.close(fake), (True, [901]))


class OwnedOnlyCloseTests(unittest.TestCase):
    """close_all must never `pkill -x agy`: only registered process groups are stopped."""
    def run_close(self, fake, owned_result):
        with patch.object(m.subprocess, 'run', side_effect=fake.run), \
             patch.object(m.time, 'time', lambda: fake.clock), patch.object(m.time, 'sleep', fake.sleep), \
             patch.object(m, 'stop_owned_agy', return_value=owned_result) as stop:
            return m.close_all(wait=5), stop

    def test_no_pkill_of_agy_and_unowned_agy_blocks_the_switch_safely(self):
        fake = FakeProcs({77: '/opt/homebrew/bin/agy --print research'})  # production-like, unregistered
        (was_app, left), stop = self.run_close(fake, {'terminated': [], 'survivors': []})
        stop.assert_called_once()
        self.assertFalse(any(c[:1] == ['pkill'] and '-x' in c for c in fake.calls))
        self.assertIn(77, fake.procs)          # still alive
        self.assertEqual(left, [77])           # caller refuses the switch: live state untouched

    def test_missing_ownership_module_terminates_nothing(self):
        with patch.dict(os.environ, {'AGY_OWNERSHIP_DIR': '/nonexistent-dir'}), \
             patch.dict('sys.modules', {'agy_ownership': None}):
            self.assertIsNone(m.stop_owned_agy())

    def test_real_stop_uses_registry_only(self):
        sys_path = str(Path(__file__).resolve().parent / 'daemon')
        with tempfile.TemporaryDirectory() as t, \
             patch.dict(os.environ, {'AGY_OWNERSHIP_DIR': sys_path, 'AGY_OWNED_DIR': t}):
            res = m.stop_owned_agy()
        self.assertEqual(res['terminated'], [])


class FakeAgyScreen:
    """tmux stand-in for an interactive agy login: login menu -> URL -> trust prompt."""
    URL = ('https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x&redirect_uri='
           'https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&scope=a+b&state=S')

    def __init__(self, on_code=None):
        self.stage, self.sent, self.on_code, self.selected = 'menu', [], on_code, 'yes'

    def capture(self):
        if self.stage == 'menu':
            return ' Select login method:\n > 1. Google OAuth\n   2. Use a Google Cloud project\n'
        if self.stage == 'url':
            return (' Your browser should open automatically. If not:\n ' + self.URL[:60] + '\n ' + self.URL[60:] +
                    '\n If you aren\'t automatically redirected, paste the authorization code below:\n')
        if self.stage == 'trust':
            return (' > Yes, I trust this folder\n   No, exit\n' if self.selected == 'yes'
                    else '   Yes, I trust this folder\n > No, exit\n')
        return ''

    def send(self, keys, literal=False):
        self.sent.append(keys)
        if self.stage == 'menu' and keys == 'Enter':
            self.stage = 'url'
        elif self.stage == 'url' and literal:
            self.code = keys
        elif self.stage == 'url' and keys == 'Enter':
            self.stage = 'trust'
            if self.on_code:
                self.on_code(self.code)
        elif self.stage == 'trust' and keys == 'Down':
            self.selected = 'no'
        elif self.stage == 'trust' and keys == 'Enter' and self.selected == 'no':
            self.stage = 'gone'


class WebLoginTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(m, 'HOME', temp))
        self.stack.enter_context(patch.object(m, 'META', os.path.join(temp, 'profiles.json')))
        self.store = {(m.LIVE_SVC, m.LIVE_ACCT): 'C:live', **{(m.STORE_SVC, f'GEMINI_{x}'): f'{x}:old' for x in 'ABC'}}
        m.save_meta({'profiles': {f'GEMINI_{x}': {'id': x} for x in 'ABC'}, 'active': 'GEMINI_C'})
        self.stack.enter_context(patch.object(m, 'identity', side_effect=lambda x: x.split(':')[0] if x else None))
        self.stack.enter_context(patch.object(m, 'kc_read', side_effect=lambda s, a: self.store.get((s, a))))
        self.stack.enter_context(patch.object(m, 'kc_write', side_effect=lambda s, a, v: self.store.__setitem__((s, a), v)))
        self.stack.enter_context(patch.object(m, 'kc_delete', side_effect=lambda s, a: self.store.pop((s, a), None)))
        self.closer = self.stack.enter_context(patch.object(m, 'close_all', return_value=(True, [])))
        self.stack.enter_context(patch.object(m, 'agy_running', return_value=[]))
        self.run = self.stack.enter_context(patch.object(m.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)))
        self.popen = self.stack.enter_context(patch.object(m.subprocess, 'Popen'))  # tab closing never waited for
        self.stack.enter_context(patch.object(m.time, 'sleep'))
        self.screen = FakeAgyScreen(on_code=lambda code: self.store.__setitem__((m.LIVE_SVC, m.LIVE_ACCT), self.new_login))
        self.new_login = 'D:new'
        self.live_at_agy_start = 'unset'

        def tmux_start():
            self.live_at_agy_start = self.store.get((m.LIVE_SVC, m.LIVE_ACCT))
            self.screen.stage = 'menu'
        self.stack.enter_context(patch.object(m, 'tmux_start', side_effect=tmux_start))
        self.stack.enter_context(patch.object(m, 'tmux_capture', side_effect=lambda: self.screen.capture()))
        self.stack.enter_context(patch.object(m, 'tmux_send', side_effect=self.screen.send))
        self.stack.enter_context(patch.object(m, 'tmux_stop'))
        self.out = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.out))

    def last(self):
        return json.loads(self.out.getvalue().strip().splitlines()[-1])

    def start(self, label='GEMINI_D'):
        m.cmd_login_start(types.SimpleNamespace(label=label, json=True))

    def finish(self, code='4/0AXlqoi7-test-code_abcdef'):
        m.cmd_login_finish(types.SimpleNamespace(code=code, json=True))

    def test_start_returns_joined_login_url_and_parks_previous_account(self):
        self.start()
        self.assertEqual(self.last()['url'], FakeAgyScreen.URL)
        self.assertEqual(self.store[(m.STORE_SVC, 'GEMINI_C')], 'C:live')
        self.assertIsNone(self.live_at_agy_start)  # agy must find no account to offer a sign-in
        # ...but while the user signs in, other agy runs get the previous account, not a browser tab
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'C:live')
        self.closer.assert_called_once()

    def test_signing_in_as_the_previous_account_is_refused_and_restored(self):
        self.new_login = 'C:fresh'
        self.start()
        with self.assertRaises(SystemExit) as e:
            self.finish()
        self.assertIn('GEMINI_C', str(e.exception))
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'C:live')
        self.assertNotIn('GEMINI_D', m.load_meta()['profiles'])

    def test_finish_enrolls_new_account_and_restores_previous(self):
        self.start()
        self.finish()
        self.assertEqual(self.last()['enrolled'], 'GEMINI_D')
        self.assertEqual(self.last()['restored'], 'GEMINI_C')
        self.assertEqual(self.store[(m.STORE_SVC, 'GEMINI_D')], 'D:new')
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'C:live')
        self.assertIn('4/0AXlqoi7-test-code_abcdef', self.screen.sent)
        self.assertEqual(self.screen.stage, 'gone')
        self.run.assert_any_call(['open', '-a', m.APP])

    def test_already_enrolled_account_is_rejected_and_previous_restored(self):
        self.new_login = 'A:again'
        self.start()
        with self.assertRaises(SystemExit):
            self.finish()
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'C:live')
        self.assertNotIn('GEMINI_D', m.load_meta()['profiles'])

    def test_cancel_restores_previous(self):
        self.start()
        m.cmd_login_cancel(types.SimpleNamespace(json=True))
        self.assertEqual(self.store[(m.LIVE_SVC, m.LIVE_ACCT)], 'C:live')
        self.assertEqual(self.last()['restored'], 'GEMINI_C')

    def osascripts(self):
        return [c.args[0] for c in self.popen.call_args_list if c.args and c.args[0][0] == 'osascript']

    def test_sign_in_tabs_are_closed_when_login_ends(self):
        for end in (self.finish, lambda: m.cmd_login_cancel(types.SimpleNamespace(json=True))):
            self.popen.reset_mock()
            self.store.pop((m.STORE_SVC, 'GEMINI_D'), None)
            m.save_meta({'profiles': {f'GEMINI_{x}': {'id': x} for x in 'ABC'}, 'active': 'GEMINI_C'})
            self.start()
            self.assertEqual(self.osascripts(), [])  # the user still needs the page to copy the code
            end()
            scripts = ' '.join(a[2] for a in self.osascripts())
            self.assertIn('application "Safari" is running', scripts)
            self.assertIn('application "Google Chrome" is running', scripts)
            self.assertIn('client_id=' + m.AGY_OAUTH_CLIENT, scripts)
            self.assertIn('https://antigravity.google/oauth-callback', scripts)


    def test_bad_or_existing_label_refused_before_touching_anything(self):
        for label in ('gemini_d', 'GEMINI_C', 'GEMINI_D; rm'):
            with self.assertRaises(SystemExit):
                self.start(label)
        self.closer.assert_not_called()


class RemoveTests(WebLoginTests):
    def remove(self, label):
        m.cmd_remove(types.SimpleNamespace(label=label, json=True))

    def test_remove_deletes_store_copy_and_metadata(self):
        self.remove('GEMINI_A')
        self.assertNotIn((m.STORE_SVC, 'GEMINI_A'), self.store)
        self.assertNotIn('GEMINI_A', m.load_meta()['profiles'])
        self.assertEqual(self.last()['removed'], 'GEMINI_A')

    def test_live_account_cannot_be_removed(self):
        with self.assertRaises(SystemExit):
            self.remove('GEMINI_C')
        self.assertIn((m.STORE_SVC, 'GEMINI_C'), self.store)
        self.assertIn('GEMINI_C', m.load_meta()['profiles'])

    def test_unknown_label(self):
        with self.assertRaises(SystemExit):
            self.remove('GEMINI_Z')


class LoginUrlTests(unittest.TestCase):
    def test_agy_1_2_14_blank_line_before_wrapped_url(self):
        screen = (" Your browser should open automatically. If not:                 \n\n"
                  " https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=x\n"
                  " o.profile+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcclog&state=s\n\n"
                  " If you aren't automatically redirected, paste the authorization code below:\n")
        self.assertEqual(m.login_url(screen), "https://accounts.google.com/o/oauth2/auth?access_type=offline"
                         "&client_id=xo.profile+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcclog&state=s")

    def test_older_layout_without_blank_line(self):
        screen = " If not:\n https://accounts.google.com/a\n b\n paste the authorization code\n"
        self.assertEqual(m.login_url(screen), "https://accounts.google.com/ab")


class CloseTests(unittest.TestCase):
    def test_quit_timeout_preserves_running_processes_for_caller(self):
        with patch.object(m, 'app_running', return_value=True), \
             patch.object(m, 'agy_running', return_value=[123]), \
             patch.object(m.subprocess, 'run', side_effect=[
                 subprocess.TimeoutExpired('osascript', 30), types.SimpleNamespace(stdout='123\n')]) as run:
            self.assertEqual(m.close_all(), (True, [123]))
            self.assertFalse(any('pkill' in call.args[0] for call in run.call_args_list))


class LoginBrowserTests(unittest.TestCase):
    def test_signed_out_agy_cannot_open_a_browser_by_itself(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'HOME', temp), \
                patch.object(m.subprocess, 'run') as run:
            m.tmux_start()
            cmd = run.call_args_list[-1].args[0]
            self.assertEqual(cmd[:2], ['tmux', 'new-session'])
            env = cmd[cmd.index('env') + 1:]
            self.assertIn('BROWSER=true', env)
            shim_dir = next(e for e in env if e.startswith('PATH=')).split('=', 1)[1].split(':')[0]
            shim = os.path.join(shim_dir, 'open')
            self.assertTrue(env[-1].endswith('agy'))
            self.assertTrue(os.access(shim, os.X_OK))
        # run the shim for real, outside the patch: it must do nothing
        with tempfile.TemporaryDirectory() as temp, patch.object(m, 'HOME', temp):
            shim = os.path.join(m.no_browser_path(), 'open')
            r = subprocess.run([shim, 'https://accounts.google.com/o/oauth2/auth'], capture_output=True)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''))


if __name__ == '__main__': unittest.main()
