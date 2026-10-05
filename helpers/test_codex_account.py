"""No live Keychain, network or ~/.codex access: fake Keychain + temporary CODEX_HOME directories."""
import base64
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('codex_account', str(Path(__file__).with_name('codex-account')))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)


def jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
    return f'{enc({"alg": "none"})}.{enc(claims)}.sig'


def auth(account, refresh='r1', access='a1', mode='chatgpt'):
    return {'auth_mode': mode, 'OPENAI_API_KEY': None,
            'tokens': {'id_token': jwt({'sub': f'user-{account}', 'email': f'{account}@example.com'}),
                       'access_token': access, 'refresh_token': refresh, 'account_id': f'acct-{account}'},
            'last_refresh': '2026-09-26T10:00:00Z'}


class Base(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.live = root / 'codex'
        self.live.mkdir()
        self.store = {}
        self.stack.enter_context(patch.dict(os.environ, {'CODEX_HOME': str(self.live)}))
        self.stack.enter_context(patch.object(m, 'HOME', str(root / 'meta')))
        self.stack.enter_context(patch.object(m, 'META', str(root / 'meta' / 'profiles.json')))
        self.stack.enter_context(patch.object(m, 'kc_read', side_effect=lambda label: self.store.get(label)))
        self.write = self.stack.enter_context(patch.object(
            m, 'kc_write', side_effect=lambda label, value: self.store.__setitem__(label, value)))
        self.stack.enter_context(patch.object(m, 'codex_running', return_value=[]))
        self.out = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.out))

    def put_live(self, data):
        (self.live / 'auth.json').write_text(json.dumps(data))

    def live_data(self):
        return json.loads((self.live / 'auth.json').read_text())

    def enroll(self, label, data):
        self.put_live(data)
        m.cmd_enroll(types.SimpleNamespace(label=label, source=None, json=True))

    def use(self, label, force=False):
        m.cmd_use(types.SimpleNamespace(label=label, force=force, json=True))

    def result(self):
        return json.loads(self.out.getvalue().strip().splitlines()[-1])


class IdentityTests(unittest.TestCase):
    def test_chatgpt_login_has_stable_opaque_id(self):
        ident = m.identity(auth('A'))
        self.assertRegex(ident, r'^[0-9a-f]{12}$')
        self.assertEqual(ident, m.identity(auth('A', refresh='rotated', access='new')))
        self.assertNotEqual(ident, m.identity(auth('B')))

    def test_unusable_logins_have_no_identity(self):
        no_refresh = auth('A', refresh='')
        self.assertIsNone(m.identity(no_refresh))
        self.assertIsNone(m.identity(auth('A', mode='apikey')))
        self.assertIsNone(m.identity(None))


class EncodingTests(unittest.TestCase):
    def test_real_sized_login_fits_security_line_limit(self):
        big = auth('A', access=jwt({'claims': 'x' * 1800, 'n': list(range(300))}))
        big['tokens']['id_token'] = jwt({'sub': 'user-A', 'profile': 'y' * 1500, 'n': list(range(200))})
        value = m.encode(big)
        self.assertLess(len(value), m.MAX_VALUE)
        self.assertEqual(m.decode(value), big)

    def test_legacy_uncompressed_values_still_decode(self):
        data = auth('A')
        legacy = 'b64:' + base64.b64encode(json.dumps(data).encode()).decode()
        self.assertEqual(m.decode(legacy), data)

    def test_oversized_value_is_refused_not_truncated(self):
        with patch.object(m.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                m.kc_write('CODEX_A', 'z64:' + 'A' * m.MAX_VALUE)
            run.assert_not_called()


class EnrollTests(Base):
    def test_enroll_copies_live_without_touching_it(self):
        self.put_live(auth('A'))
        before = (self.live / 'auth.json').read_bytes()
        m.cmd_enroll(types.SimpleNamespace(label='CODEX_A', source=None, json=True))
        self.assertEqual(m.decode(self.store['CODEX_A'])['tokens']['account_id'], 'acct-A')
        self.assertEqual((self.live / 'auth.json').read_bytes(), before)
        self.assertEqual(self.result(), {'enrolled': 'CODEX_A', 'id': m.identity(auth('A'))})

    def test_enroll_from_isolated_home(self):
        other = self.live.parent / 'login'
        other.mkdir()
        (other / 'auth.json').write_text(json.dumps(auth('B')))
        m.cmd_enroll(types.SimpleNamespace(label='CODEX_B', source=str(other), json=True))
        self.assertIn('CODEX_B', m.load_meta()['profiles'])
        self.assertFalse((self.live / 'auth.json').exists())

    def test_same_account_cannot_be_enrolled_twice(self):
        self.enroll('CODEX_A', auth('A'))
        with self.assertRaises(SystemExit):
            m.cmd_enroll(types.SimpleNamespace(label='CODEX_X', source=None, json=True))

    def test_stored_value_has_no_secret_in_output(self):
        self.enroll('CODEX_A', auth('A', refresh='SECRET-R', access='SECRET-A'))
        self.assertNotIn('SECRET', self.out.getvalue())


class UseTests(Base):
    def setUp(self):
        super().setUp()
        self.enroll('CODEX_A', auth('A'))
        self.enroll('CODEX_B', auth('B'))
        self.put_live(auth('A', refresh='r-rotated', access='a-new'))  # Codex refreshed A since enrolment.
        self.out.truncate(0); self.out.seek(0)

    def test_switch_saves_rotated_live_token_then_activates_target(self):
        self.use('CODEX_B')
        self.assertEqual(self.live_data()['tokens']['account_id'], 'acct-B')
        self.assertEqual(m.decode(self.store['CODEX_A'])['tokens']['refresh_token'], 'r-rotated')
        self.assertEqual(stat.S_IMODE((self.live / 'auth.json').stat().st_mode), 0o600)
        self.assertEqual(self.result()['previous'], 'CODEX_A')
        self.assertEqual(self.result()['activation'], 'OK')

    def test_switching_back_uses_saved_rotated_token(self):
        self.use('CODEX_B')
        self.use('CODEX_A')
        self.assertEqual(self.live_data()['tokens']['refresh_token'], 'r-rotated')

    def test_same_profile_keeps_newer_live_token(self):
        self.use('CODEX_A')
        self.assertEqual(self.live_data()['tokens']['refresh_token'], 'r-rotated')
        self.assertEqual(m.decode(self.store['CODEX_A'])['tokens']['refresh_token'], 'r-rotated')

    def test_unenrolled_live_login_is_never_overwritten(self):
        self.put_live(auth('Z'))
        with self.assertRaises(SystemExit):
            self.use('CODEX_B')
        self.assertEqual(self.live_data()['tokens']['account_id'], 'acct-Z')

    def test_force_does_not_bypass_unenrolled_protection(self):
        self.put_live(auth('Z'))
        with self.assertRaises(SystemExit):
            self.use('CODEX_B', force=True)
        self.assertEqual(self.live_data()['tokens']['account_id'], 'acct-Z')

    def test_malformed_target_leaves_live_untouched(self):
        self.store['CODEX_B'] = 'b64:not-json'
        before = (self.live / 'auth.json').read_bytes()
        with self.assertRaises(SystemExit):
            self.use('CODEX_B')
        self.assertEqual((self.live / 'auth.json').read_bytes(), before)

    def test_failed_activation_rolls_back(self):
        real = m.write_auth
        def failing(home, text):
            if 'acct-B' in text:
                raise OSError('fixture')
            real(home, text)
        with patch.object(m, 'write_auth', side_effect=failing):
            with self.assertRaises(SystemExit) as error:
                self.use('CODEX_B')
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.live_data()['tokens']['refresh_token'], 'r-rotated')
        self.assertEqual(self.result()['activation'], 'ROLLED_BACK')

    def test_running_codex_is_reported_not_killed(self):
        with patch.object(m, 'codex_running', return_value=[4242]):
            self.use('CODEX_B')
        self.assertEqual(self.result()['codex_running'], [4242])
        self.assertEqual(self.live_data()['tokens']['account_id'], 'acct-B')


class WarmTests(Base):
    """Warm-up runs `codex exec` as an idle account in a throwaway CODEX_HOME; the live login is never touched."""
    def setUp(self):
        super().setUp()
        self.enroll('CODEX_B', auth('b'))
        self.enroll('CODEX_A', auth('a'))  # A is live
        self.out.truncate(0); self.out.seek(0)
        self.calls = []
        self.homes = []

    def fake_exec(self, refreshed=None, text='CODEX_ACCOUNT_SMOKE_OK'):
        def run(cmd, **kw):
            home = Path(kw['env']['CODEX_HOME'])
            self.calls.append((cmd, json.loads((home / 'auth.json').read_text())))
            self.homes.append(kw['env']['CODEX_HOME'])
            if refreshed:
                (home / 'auth.json').write_text(json.dumps(refreshed))
            return types.SimpleNamespace(returncode=0, stdout=text, stderr='')
        return patch.object(m.subprocess, 'run', side_effect=run)

    def warm(self, label):
        m.cmd_warm(types.SimpleNamespace(label=label, timeout=60, json=True))
        return self.result()

    def test_runs_as_target_in_private_home_and_leaves_live_alone(self):
        before = (self.live / 'auth.json').read_text()
        with self.fake_exec():
            result = self.warm('CODEX_B')
        cmd, used = self.calls[0]
        self.assertEqual(used['tokens']['account_id'], 'acct-b')
        self.assertIn('exec', cmd)
        self.assertEqual((self.live / 'auth.json').read_text(), before)
        self.assertEqual((result['warmed'], result['status']), ('CODEX_B', 'PASS'))

    def test_refreshed_tokens_are_saved_back(self):
        with self.fake_exec(refreshed=auth('b', refresh='r2', access='a2')):
            self.warm('CODEX_B')
        self.assertEqual(m.decode(self.store['CODEX_B'])['tokens']['refresh_token'], 'r2')

    def test_other_identity_written_in_home_is_not_stored(self):
        with self.fake_exec(refreshed=auth('z')):
            self.warm('CODEX_B')
        self.assertEqual(m.decode(self.store['CODEX_B'])['tokens']['account_id'], 'acct-b')

    def test_live_account_is_warmed_in_the_live_home(self):
        # A private copy would refresh the single-use refresh token and invalidate the live login.
        with self.fake_exec():
            result = self.warm('CODEX_A')
        cmd, used = self.calls[0]
        self.assertEqual(used['tokens']['account_id'], 'acct-a')
        self.assertEqual(self.homes[0], str(self.live))
        self.assertEqual(result['status'], 'PASS')

    def test_quota_error_is_reported(self):
        with self.fake_exec(text="You've hit your usage limit"), self.assertRaises(SystemExit):
            self.warm('CODEX_B')
        self.assertEqual(self.result()['status'], 'PROFILE_ACTIVATED_BUT_ACCOUNT_UNAVAILABLE')


class RemoveTests(Base):
    def setUp(self):
        super().setUp()
        self.enroll('CODEX_B', auth('b'))
        self.enroll('CODEX_A', auth('a'))  # A is live
        self.deleted = []
        self.stack.enter_context(patch.object(m, 'kc_delete', side_effect=lambda label: (self.deleted.append(label), self.store.pop(label, None))))

    def remove(self, label):
        m.cmd_remove(types.SimpleNamespace(label=label, json=True))

    def test_removes_stored_copy_and_metadata(self):
        self.remove('CODEX_B')
        self.assertEqual(self.deleted, ['CODEX_B'])
        self.assertNotIn('CODEX_B', m.load_meta()['profiles'])

    def test_refuses_live_account(self):
        with self.assertRaises(SystemExit) as e: self.remove('CODEX_A')
        self.assertIn('live account', str(e.exception.code))
        self.assertEqual(self.deleted, [])

    def test_refuses_unknown_label(self):
        with self.assertRaises(SystemExit): self.remove('CODEX_Z')


class FakeProcs:
    """ps/pgrep/osascript stand-in plus os.kill. ChatGPT may need a few ticks to finish quitting,
    and it hangs forever if its bundled Codex is killed under it (seen 2026-09-27)."""
    MAIN = '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'
    SERVER = '/Applications/ChatGPT.app/Contents/Resources/codex app-server'
    CLI = '/opt/homebrew/lib/node_modules/@openai/codex/vendor/bin/codex'

    def __init__(self, procs, ignores_term=(), quit_ticks=0):
        self.procs, self.ignores_term, self.clock, self.calls = dict(procs), set(ignores_term), 0.0, []
        self.quitting_at, self.quit_ticks, self.killed, self.app_hung = None, quit_ticks, [], False

    def app_pids(self):
        return [p for p, c in self.procs.items() if c.startswith('/Applications/ChatGPT.app/')]

    def match(self, flag, pattern):
        if flag == '-x':
            return [p for p, c in self.procs.items() if c.split()[0].rsplit('/', 1)[-1] == pattern]
        import re
        return [p for p, c in self.procs.items() if re.search(pattern, c)]

    def run(self, args, **kw):
        self.calls.append(args)
        if args[0] == 'osascript':
            self.quitting_at = self.clock
            self.tick()
            return types.SimpleNamespace(returncode=0, stdout='')
        if args[0] == 'pgrep':
            pids = self.match(args[1], args[2])
            return types.SimpleNamespace(returncode=0 if pids else 1, stdout=''.join(f'{p}\n' for p in pids))
        if args[0] == 'ps':
            return types.SimpleNamespace(returncode=0, stdout=''.join(f'{p} {c}\n' for p, c in self.procs.items()))
        raise AssertionError(args)

    def kill(self, pid, sig):
        self.killed.append(pid)
        if self.procs.get(pid) == self.MAIN:  # ChatGPT exits cleanly on SIGTERM (seen 2026-09-27)
            for p in self.app_pids():
                del self.procs[p]
            return
        if self.procs.get(pid, '').startswith('/Applications/ChatGPT.app/') and self.quitting_at is not None:
            self.app_hung = True
        if pid not in self.ignores_term:
            self.procs.pop(pid, None)

    def tick(self):
        if self.quitting_at is not None and not self.app_hung and self.clock - self.quitting_at >= self.quit_ticks:
            for p in self.app_pids():
                del self.procs[p]

    def sleep(self, s):
        self.clock += s
        self.tick()


class CloseAllTests(unittest.TestCase):
    def close(self, fake, wait=10):
        with patch.object(m.subprocess, 'run', side_effect=fake.run), patch.object(m.os, 'kill', fake.kill), \
             patch.object(m.time, 'time', lambda: fake.clock), patch.object(m.time, 'sleep', fake.sleep):
            return m.close_all(wait=wait)

    def test_sigterms_chatgpt_main_then_cli_without_quit_event(self):
        # The Quit AppleEvent is approved but ChatGPT never exits; SIGTERM does (2026-09-27).
        fake = FakeProcs({10: FakeProcs.MAIN, 11: FakeProcs.SERVER, 20: FakeProcs.CLI})
        self.assertEqual(self.close(fake), (True, []))
        self.assertEqual(fake.killed, [10, 20])
        self.assertFalse(any(c[0] == 'osascript' for c in fake.calls))

    def test_bundled_codex_is_left_to_chatgpt(self):
        fake = FakeProcs({10: FakeProcs.MAIN, 11: FakeProcs.SERVER})
        self.assertEqual(self.close(fake), (True, []))
        self.assertNotIn(11, fake.killed)
        self.assertFalse(fake.app_hung)

    def test_chatgpt_surviving_sigterm_blocks_switch_before_cli_is_touched(self):
        fake = FakeProcs({10: FakeProcs.MAIN, 20: FakeProcs.CLI}, ignores_term={10})
        fake.kill = lambda pid, sig, f=fake: f.killed.append(pid)  # ChatGPT ignores SIGTERM
        self.assertEqual(self.close(fake, wait=5), (True, [10, 20]))
        self.assertEqual(fake.killed, [10])

    def test_unrelated_process_mentioning_app_path_is_ignored(self):
        # A shell whose command line merely contains the path (e.g. a monitor script) is not ChatGPT.
        fake = FakeProcs({30: "/bin/zsh -c pgrep -f '/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'"})
        self.assertEqual(self.close(fake), (False, []))
        self.assertEqual(fake.killed, [])

    def test_cli_only_does_not_touch_app(self):
        fake = FakeProcs({20: FakeProcs.CLI})
        self.assertEqual(self.close(fake), (False, []))
        self.assertEqual(fake.killed, [20])

    def test_survivor_is_reported(self):
        fake = FakeProcs({20: FakeProcs.CLI}, ignores_term={20})
        self.assertEqual(self.close(fake), (False, [20]))


class CloseAllUseTests(Base):
    def setUp(self):
        super().setUp()
        self.enroll('CODEX_A', auth('A'))
        self.enroll('CODEX_B', auth('B'))
        self.put_live(auth('A'))
        self.closer = self.stack.enter_context(patch.object(m, 'close_all', return_value=(True, [])))
        self.opener = self.stack.enter_context(patch.object(m.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)))
        self.out.truncate(0); self.out.seek(0)

    def use_all(self, label):
        m.cmd_use(types.SimpleNamespace(label=label, close_all=True, json=True))

    def test_closes_switches_and_reopens_app(self):
        self.use_all('CODEX_B')
        self.closer.assert_called_once()
        self.assertEqual(self.live_data()['tokens']['account_id'], 'acct-B')
        self.opener.assert_called_once_with(['open', '-b', 'com.openai.codex'])
        self.assertTrue(self.result()['app_reopened'])

    def test_json_output_is_a_single_document(self):
        self.use_all('CODEX_B')
        doc = json.loads(self.out.getvalue())
        self.assertEqual((doc['activation'], doc['selected'], doc['app_reopened']), ('OK', 'CODEX_B', True))

    def test_close_timeout_leaves_live_and_reopens_app(self):
        self.closer.return_value = (True, [20])
        with self.assertRaises(SystemExit) as e:
            self.use_all('CODEX_B')
        self.assertIn('could not stop', str(e.exception.code))
        self.assertEqual(self.live_data()['tokens']['account_id'], 'acct-A')
        self.opener.assert_called_once_with(['open', '-b', 'com.openai.codex'])

    def test_broken_target_never_closes_anything(self):
        self.store['CODEX_B'] = 'z64:broken'
        with self.assertRaises(SystemExit):
            self.use_all('CODEX_B')
        self.closer.assert_not_called()
        self.opener.assert_not_called()

    def test_app_not_running_is_not_opened(self):
        self.closer.return_value = (False, [])
        self.use_all('CODEX_B')
        self.opener.assert_not_called()
        self.assertFalse(self.result()['app_reopened'])


class LoginTests(Base):
    def test_device_login_uses_isolated_home_and_cleans_it(self):
        seen = {}
        def fake_codex(args, env, **kw):
            home = Path(env['CODEX_HOME'])
            seen.update(args=args, home=home, mode=stat.S_IMODE(home.stat().st_mode))
            (home / 'auth.json').write_text(json.dumps(auth('C')))
            return types.SimpleNamespace(returncode=0)
        with patch.object(m.subprocess, 'run', side_effect=fake_codex):
            m.cmd_login(types.SimpleNamespace(label='CODEX_C', json=True))
        self.assertEqual(seen['args'][-2:], ['login', '--device-auth'])
        self.assertEqual(seen['mode'], 0o700)
        self.assertNotEqual(seen['home'], self.live)
        self.assertFalse(seen['home'].exists())
        self.assertIn('CODEX_C', m.load_meta()['profiles'])
        self.assertFalse((self.live / 'auth.json').exists())

    def test_failed_login_still_removes_temp_home(self):
        seen = {}
        def fake_codex(args, env, **kw):
            seen['home'] = Path(env['CODEX_HOME'])
            (seen['home'] / 'auth.json').write_text('{"partial": true}')
            return types.SimpleNamespace(returncode=1)
        with patch.object(m.subprocess, 'run', side_effect=fake_codex):
            with self.assertRaises(SystemExit):
                m.cmd_login(types.SimpleNamespace(label='CODEX_C', json=True))
        self.assertFalse(seen['home'].exists())


class LoginUxTests(Base):
    def test_ctrl_c_cancels_cleanly_and_removes_temp_home(self):
        seen = {}
        def interrupted(args, env, **kw):
            seen['home'] = Path(env['CODEX_HOME'])
            raise KeyboardInterrupt
        err = io.StringIO()
        with patch.object(m.subprocess, 'run', side_effect=interrupted), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as e:
                m.cmd_login(types.SimpleNamespace(label='CODEX_C', json=False))
        self.assertIn('cancelled', str(e.exception.code))
        self.assertFalse(seen['home'].exists())

    def test_login_warns_to_use_private_window_before_starting(self):
        order = []
        def fake(args, env, **kw):
            order.append(('codex', self.out.getvalue()))
            (Path(env['CODEX_HOME']) / 'auth.json').write_text(json.dumps(auth('C')))
            return types.SimpleNamespace(returncode=0)
        with patch.object(m.subprocess, 'run', side_effect=fake):
            m.cmd_login(types.SimpleNamespace(label='CODEX_C', json=False))
        self.assertIn('private', order[0][1].lower())


class UsageErrorTests(unittest.TestCase):
    def fail_with(self, code, body):
        err = m.urllib.error.HTTPError(m.USAGE_URL, code, 'x', {}, io.BytesIO(body))
        with patch.object(m.urllib.request, 'urlopen', side_effect=err):
            with self.assertRaises(m.UsageError) as e:
                m.fetch_usage('t', 'a')
        return str(e.exception)

    def test_revoked_token_is_distinguished_from_expired(self):
        self.assertEqual(self.fail_with(401, b'{"error":{"code":"token_revoked"},"status":401}'), 'TOKEN_REVOKED')
        self.assertEqual(self.fail_with(401, b'{"error":{"code":"token_expired"}}'), 'TOKEN_EXPIRED')
        self.assertEqual(self.fail_with(401, b'<html>nope</html>'), 'TOKEN_EXPIRED')
        self.assertEqual(self.fail_with(500, b''), 'HTTP_500')


class QuotaTests(Base):
    USAGE = {'plan_type': 'plus', 'email': 'hidden@example.com',
             'rate_limit': {'limit_reached': False,
                            'primary_window': {'used_percent': 12, 'limit_window_seconds': 18000, 'reset_at': 1790454707},
                            'secondary_window': {'used_percent': 16, 'limit_window_seconds': 604800, 'reset_at': 1791021866}}}

    def setUp(self):
        super().setUp()
        self.enroll('CODEX_A', auth('A', access='stored-A'))
        self.enroll('CODEX_B', auth('B', access='stored-B'))
        self.put_live(auth('A', access='live-A'))
        self.out.truncate(0); self.out.seek(0)

    def test_active_account_reads_live_token_others_read_store(self):
        calls = []
        with patch.object(m, 'fetch_usage', side_effect=lambda token, account: calls.append((token, account)) or self.USAGE):
            m.cmd_quota(types.SimpleNamespace(label=None, json=True))
        self.assertEqual(sorted(calls), [('live-A', 'acct-A'), ('stored-B', 'acct-B')])
        rows = self.result()['profiles']
        self.assertEqual(rows['CODEX_A']['5h']['remaining_percent'], 88)
        self.assertEqual(rows['CODEX_B']['weekly']['remaining_percent'], 84)
        self.assertTrue(rows['CODEX_A']['active'])

    def test_quota_shows_login_email_from_local_id_token(self):
        with patch.object(m, 'fetch_usage', return_value=self.USAGE):
            m.cmd_quota(types.SimpleNamespace(label=None, json=True))
        rows = self.result()['profiles']
        self.assertEqual((rows['CODEX_A']['email'], rows['CODEX_B']['email']), ('A@example.com', 'B@example.com'))

    def test_email_shown_even_when_usage_fails(self):
        with patch.object(m, 'fetch_usage', side_effect=m.UsageError('TOKEN_REVOKED')):
            m.cmd_quota(types.SimpleNamespace(label='CODEX_B', json=True))
        self.assertEqual(self.result()['profiles']['CODEX_B']['email'], 'B@example.com')

    def test_quota_never_refreshes_or_prints_secrets(self):
        with patch.object(m, 'fetch_usage', return_value=self.USAGE):
            m.cmd_quota(types.SimpleNamespace(label=None, json=True))
        text = self.out.getvalue()
        for secret in ('live-A', 'stored-B', 'hidden@example.com', 'r1'):
            self.assertNotIn(secret, text)
        self.assertFalse(hasattr(m, 'refresh_tokens'))

    def test_expired_token_is_reported_not_refreshed(self):
        def expired(token, account):
            raise m.UsageError('TOKEN_EXPIRED')
        with patch.object(m, 'fetch_usage', side_effect=expired):
            m.cmd_quota(types.SimpleNamespace(label='CODEX_B', json=True))
        self.assertEqual(self.result()['profiles']['CODEX_B']['status'], 'TOKEN_EXPIRED')


class ContinueTests(Base):
    TID = '01a0aa3c-1e93-79e1-89ea-c83e3001e106'

    def write_thread(self, events):
        d = self.live / 'sessions' / '2026' / '09' / '27'
        d.mkdir(parents=True, exist_ok=True)
        lines = [{'type': 'session_meta', 'payload': {'id': self.TID, 'cwd': '/work/proj', 'originator': 'Codex Desktop', 'source': 'vscode'}}]
        lines += events
        (d / f'rollout-2026-09-27T10-00-00-{self.TID}.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in lines))

    def settings(self, **kw):
        base = {'model': 'gpt-6-astra', 'approval_policy': 'never', 'cwd': '/work/proj',
                'runtime_workspace_roots': ['/work/proj', '/work/shared'], 'reasoning_effort': 'medium'}
        return {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_settings': {**base, **kw}}}

    def context(self, sandbox='danger-full-access', model='gpt-6-sol', effort='high'):
        return {'type': 'turn_context', 'payload': {'cwd': '/work/proj', 'approval_policy': 'never', 'model': model,
                                                    'effort': effort, 'sandbox_policy': {'type': sandbox}}}

    def event(self, kind):
        return {'type': 'event_msg', 'payload': {'type': kind}}

    def plan(self):
        return m.thread_plan(m.find_rollout(self.TID))

    def test_plan_uses_project_permissions_and_latest_model(self):
        self.write_thread([self.context(), self.settings(), self.event('task_started'), self.event('turn_aborted')])
        plan = self.plan()
        self.assertEqual((plan['cwd'], plan['sandbox'], plan['approval']), ('/work/proj', 'danger-full-access', 'never'))
        self.assertEqual((plan['model'], plan['effort']), ('gpt-6-astra', 'medium'))  # settings came after the context
        self.assertFalse(plan['running'])
        cmd = m.resume_command(plan, self.TID, 'continue')
        self.assertEqual(cmd[:3], [m.CODEX, 'resume', self.TID])
        for flag, value in (('-C', '/work/proj'), ('-s', 'danger-full-access'), ('-a', 'never'), ('-m', 'gpt-6-astra'),
                            ('--add-dir', '/work/shared'), ('-c', 'model_reasoning_effort="medium"')):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertEqual(cmd[-1], 'continue')

    def test_later_turn_context_overrides_older_settings(self):
        self.write_thread([self.settings(), self.context(sandbox='workspace-write', model='gpt-6-sol', effort='high')])
        plan = self.plan()
        self.assertEqual((plan['sandbox'], plan['model'], plan['effort']), ('workspace-write', 'gpt-6-sol', 'high'))

    def test_running_turn_is_detected(self):
        self.write_thread([self.settings(), self.context(), self.event('task_started')])
        self.assertTrue(self.plan()['running'])

    def test_unknown_thread(self):
        self.assertIsNone(m.find_rollout('01a0ffff-0000-0000-0000-000000000000'))

    def run_continue(self, **kw):
        args = {'thread': self.TID, 'prompt': 'continue', 'dry_run': False, 'force': False, 'json': True, 'model': None, 'close_app': False, **kw}
        m.cmd_continue(types.SimpleNamespace(**args))

    def test_model_override_wins_over_thread_settings(self):
        self.write_thread([self.settings(model='gpt-6-luna'), self.context(), self.event('turn_aborted')])
        with patch.object(m, 'app_running', return_value=True):
            self.run_continue(dry_run=True, model='gpt-6-astra')
        self.assertEqual(self.result()['model'], 'gpt-6-astra')
        self.assertIn('-m gpt-6-astra', self.result()['command'])

    def test_refuses_while_chatgpt_is_open(self):
        self.write_thread([self.settings(), self.context(), self.event('task_complete')])
        with patch.object(m, 'app_running', return_value=True), patch.object(m.subprocess, 'run') as run:
            with self.assertRaises(SystemExit) as e:
                self.run_continue()
        self.assertIn('ChatGPT', str(e.exception.code))
        run.assert_not_called()

    def test_refuses_a_turn_a_codex_cli_is_still_running(self):
        self.write_thread([self.settings(), self.context(), self.event('task_started')])
        calls = []
        def fake(args, **kw):
            calls.append(args[:2])
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')  # pgrep: a CLI resumes this thread
        with patch.object(m, 'app_running', return_value=False), patch.object(m.subprocess, 'run', side_effect=fake):
            with self.assertRaises(SystemExit):
                self.run_continue()
        self.assertEqual(calls, [['pgrep', '-f']])  # nothing launched

    def test_turn_whose_process_was_killed_is_resumed(self):
        # 2026-10-05 19:44: the account switch killed the resumed CLI mid-turn; the rollout still ends with task_started.
        self.write_thread([self.settings(), self.context(), self.event('task_started')])
        launched = []
        def fake(args, **kw):
            launched.append(args[:2])
            return types.SimpleNamespace(returncode=1 if args[0] in ('pgrep',) or args[:2] == ['tmux', 'has-session'] else 0,
                                         stdout='', stderr='')
        with patch.object(m, 'app_running', return_value=False), patch.object(m.subprocess, 'run', side_effect=fake), \
             patch.object(m.os.path, 'isdir', return_value=True), patch.object(m, 'notify_rotator', return_value=True):
            self.run_continue()
        self.assertIn(['tmux', 'new-session'], launched)

    def test_app_closed_for_a_continue_is_reopened_even_when_it_fails(self):
        self.write_thread([self.settings(), self.context(), self.event('turn_aborted')])
        running, opened = {'app': True}, []
        def stop():
            running['app'] = False
            return []
        def fake(args, **kw):
            if args[:2] == ['open', '-b']:
                opened.append(args)
            return types.SimpleNamespace(returncode=1, stdout='', stderr='')
        with patch.object(m, 'app_running', side_effect=lambda: running['app']), patch.object(m, 'stop_app', side_effect=stop), \
             patch.object(m.subprocess, 'run', side_effect=fake), patch.object(m.os.path, 'isdir', return_value=False):
            with self.assertRaises(SystemExit):  # project folder missing: fails after ChatGPT was closed
                self.run_continue(close_app=True)
        self.assertEqual(opened, [['open', '-b', m.APP_ID]])

    def test_dry_run_only_prints(self):
        self.write_thread([self.settings(), self.context(), self.event('turn_aborted')])
        with patch.object(m, 'app_running', return_value=True), patch.object(m.subprocess, 'run') as run:
            self.run_continue(dry_run=True)
        run.assert_not_called()
        self.assertEqual(self.result()['cwd'], '/work/proj')

    def test_close_app_quits_chatgpt_launches_then_reopens(self):
        self.write_thread([self.settings(), self.context(), self.event('turn_aborted')])
        order = []
        running = {'app': True}
        def fake(args, **kw):
            order.append(args[:2])
            return types.SimpleNamespace(returncode=0 if args[:2] != ['tmux', 'has-session'] else 1, stdout='', stderr='')
        def stop():
            order.append(['stop-app'])
            running['app'] = False
            return []
        with patch.object(m, 'app_running', side_effect=lambda: running['app']), patch.object(m, 'stop_app', side_effect=stop), \
             patch.object(m.subprocess, 'run', side_effect=fake), patch.object(m.os.path, 'isdir', return_value=True), \
             patch.object(m, 'notify_rotator', return_value=True), patch.object(m, 'wait_cli', return_value=True):
            self.run_continue(close_app=True)
        self.assertEqual(order[0], ['stop-app'])
        self.assertEqual(order[-1], ['open', '-b'])
        self.assertTrue(self.result()['app_reopened'])

    def test_close_app_failure_does_not_launch(self):
        self.write_thread([self.settings(), self.context(), self.event('turn_aborted')])
        with patch.object(m, 'app_running', return_value=True), patch.object(m, 'stop_app', return_value=[123]), \
             patch.object(m.subprocess, 'run') as run:
            with self.assertRaises(SystemExit):
                self.run_continue(close_app=True)
        run.assert_not_called()

    def test_launches_in_tmux_window_in_project_dir(self):
        self.write_thread([self.settings(), self.context(), self.event('turn_aborted')])
        calls = []
        def fake(args, **kw):
            calls.append(args)
            return types.SimpleNamespace(returncode=0 if args[:2] != ['tmux', 'has-session'] else 1, stdout='', stderr='')
        with patch.object(m, 'app_running', return_value=False), patch.object(m.subprocess, 'run', side_effect=fake), \
             patch.object(m.os.path, 'isdir', return_value=True), patch.object(m, 'notify_rotator', return_value=True) as notify:
            self.run_continue()
        notify.assert_called_once_with(self.TID, '/work/proj', 'codex-continue:codex-01e106')
        launch = calls[-1]
        self.assertEqual(launch[:2], ['tmux', 'new-session'])
        self.assertEqual(launch[launch.index('-c') + 1], '/work/proj')
        self.assertIn('codex', launch[launch.index('-s') + 1])
        self.assertIn('resume ' + self.TID, launch[-1])


class WebCodexLoginTests(Base):
    OUTPUT = ('Welcome to Codex\n\x1b[1mFollow these steps\x1b[0m\n1. Open this link\n   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n'
              '2. Enter this one-time code (expires in 15 minutes)\n   \x1b[94mZAXT-QYP2R\x1b[0m\n')

    def fake_popen(self, args, env, **kw):
        home = Path(env['CODEX_HOME'])
        kw['stdout'].write(self.OUTPUT.encode()); kw['stdout'].flush()
        self.login_home = home
        return types.SimpleNamespace(pid=4242, poll=lambda: None)

    def start(self, label='CODEX_C'):
        with patch.object(m.subprocess, 'Popen', side_effect=self.fake_popen):
            m.cmd_login_start(types.SimpleNamespace(label=label, json=True))
        return self.result()

    def status(self):
        m.cmd_login_status(types.SimpleNamespace(json=True))
        return self.result()

    def test_start_returns_device_url_and_code(self):
        r = self.start()
        self.assertEqual((r['url'], r['code'], r['label']), ('https://auth.openai.com/codex/device', 'ZAXT-QYP2R', 'CODEX_C'))
        self.assertEqual(stat.S_IMODE(self.login_home.stat().st_mode), 0o700)

    def test_status_waits_then_enrolls_and_cleans_up(self):
        self.start()
        self.assertEqual(self.status()['state'], 'waiting')
        (self.login_home / 'auth.json').write_text(json.dumps(auth('C')))
        r = self.status()
        self.assertEqual((r['state'], r['enrolled']), ('done', 'CODEX_C'))
        self.assertFalse(self.login_home.exists())
        self.assertIn('CODEX_C', m.load_meta()['profiles'])
        self.assertFalse((self.live / 'auth.json').exists())  # live untouched

    def test_relogin_same_account_for_existing_label(self):
        self.enroll('CODEX_A', auth('A', refresh='revoked'))
        self.out.truncate(0); self.out.seek(0)
        self.start('CODEX_A')
        (self.login_home / 'auth.json').write_text(json.dumps(auth('A', refresh='fresh')))
        self.assertEqual(self.status()['state'], 'done')
        self.assertEqual(m.decode(self.store['CODEX_A'])['tokens']['refresh_token'], 'fresh')

    def test_relogin_same_account_with_new_login_subject(self):
        # Same ChatGPT account (account_id + email) signed in another way gets a new `sub`: still CODEX_A.
        self.enroll('CODEX_A', auth('A', refresh='revoked'))
        self.out.truncate(0); self.out.seek(0)
        self.start('CODEX_A')
        fresh = auth('A', refresh='fresh')
        fresh['tokens']['id_token'] = jwt({'sub': 'google-oauth2|other', 'email': 'A@example.com'})
        (self.login_home / 'auth.json').write_text(json.dumps(fresh))
        r = self.status()
        self.assertEqual(r['state'], 'done')
        self.assertEqual(m.load_meta()['profiles']['CODEX_A']['id'], m.identity(fresh))

    def test_relogin_with_other_account_is_refused(self):
        self.enroll('CODEX_A', auth('A'))
        self.out.truncate(0); self.out.seek(0)
        self.start('CODEX_A')
        (self.login_home / 'auth.json').write_text(json.dumps(auth('Z')))
        r = self.status()
        self.assertEqual(r['state'], 'failed')
        self.assertEqual(m.decode(self.store['CODEX_A'])['tokens']['account_id'], 'acct-A')

    def test_cancel_stops_login_and_removes_home(self):
        self.start()
        with patch.object(m.os, 'kill') as kill:
            m.cmd_login_cancel(types.SimpleNamespace(json=True))
        kill.assert_called_once_with(4242, m.signal.SIGTERM)
        self.assertFalse(self.login_home.exists())

    def test_bad_label_refused(self):
        with self.assertRaises(SystemExit):
            self.start('codex_c;rm')


class ProbeTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(m.classify('... CODEX_ACCOUNT_SMOKE_OK ...'), 'PASS')
        self.assertEqual(m.classify("You've hit your usage limit. Try again later."), 'PROFILE_ACTIVATED_BUT_ACCOUNT_UNAVAILABLE')
        self.assertEqual(m.classify('unexpected crash'), 'FAIL')


if __name__ == '__main__':
    unittest.main()
