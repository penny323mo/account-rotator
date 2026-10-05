"""No live Keychain / network / claude: the helper's contract with fakes."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('claude_account', str(Path(__file__).with_name('claude-account')))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)

NOW = 1_800_000_000


def creds(uuid, rt='rt-1', expires_in=3600):
    return {'oauth': {'accessToken': f'at-{uuid}', 'refreshToken': rt, 'expiresAt': (NOW + expires_in) * 1000,
                      'scopes': ['user:inference', 'user:profile'], 'subscriptionType': 'pro'},
            'account': {'accountUuid': uuid, 'organizationUuid': 'org-' + uuid, 'emailAddress': f'{uuid}@x'}}


class Base(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(m, 'HOME', tmp))
        self.stack.enter_context(patch.object(m, 'META', tmp + '/profiles.json'))
        self.stack.enter_context(patch.object(m.time, 'time', return_value=NOW))
        self.store = {}
        self.live = creds('a')
        self.stack.enter_context(patch.object(m, 'kc_read', side_effect=lambda s, a: self.store.get(a)))
        self.stack.enter_context(patch.object(m, 'kc_write', side_effect=lambda label, v: self.store.__setitem__(label, v)))
        self.stack.enter_context(patch.object(m, 'kc_delete', side_effect=lambda label: self.store.pop(label, None)))
        self.stack.enter_context(patch.object(m, 'read_live', side_effect=lambda: self.live))
        self.calls = []
        self.stack.enter_context(patch.object(m, 'api_get', side_effect=self.api))
        self.refreshes = []
        self.stack.enter_context(patch.object(m, 'refresh', side_effect=self.fake_refresh))
        self.out = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.out))

    def api(self, path, access):
        self.calls.append((path, access))
        if path == '/api/oauth/profile':
            return {'account': {'uuid': access[3:]}}
        return {'five_hour': {'utilization': 63.0, 'resets_at': '2026-10-05T04:50:00Z'},
                'seven_day': {'utilization': 38.0, 'resets_at': '2026-10-06T05:00:00Z'}}

    def fake_refresh(self, oauth):
        self.refreshes.append(oauth['refreshToken'])
        return dict(oauth, accessToken=oauth['accessToken'] + '-new', refreshToken=oauth['refreshToken'] + '-new',
                    expiresAt=(NOW + 28800) * 1000)

    def run_cmd(self, fn, **kw):
        fn(types.SimpleNamespace(json=True, **kw))
        return json.loads(self.out.getvalue().strip().splitlines()[-1])

    def enroll(self, label, live):
        self.live = live
        self.run_cmd(m.cmd_enroll, label=label)


class EnrollTests(Base):
    def test_enroll_stores_live_login_and_hashed_id_only(self):
        r = self.run_cmd(m.cmd_enroll, label='CLAUDE_A')
        self.assertEqual(r['enrolled'], 'CLAUDE_A')
        self.assertEqual(m.decode(self.store['CLAUDE_A']), self.live)
        meta = json.dumps(m.load_meta())
        self.assertNotIn('at-a', meta)
        self.assertNotIn('rt-1', meta)

    def test_same_account_cannot_be_enrolled_twice(self):
        self.run_cmd(m.cmd_enroll, label='CLAUDE_A')
        with self.assertRaises(SystemExit):
            self.run_cmd(m.cmd_enroll, label='CLAUDE_B')

    def test_token_of_another_account_is_refused(self):
        self.live['oauth']['accessToken'] = 'at-someone-else'
        with self.assertRaises(SystemExit):
            self.run_cmd(m.cmd_enroll, label='CLAUDE_A')


class QuotaTests(Base):
    def test_rows_report_remaining_percent_like_codex(self):
        self.enroll('CLAUDE_A', creds('a'))
        r = self.run_cmd(m.cmd_quota, label=None)['profiles']['CLAUDE_A']
        self.assertEqual((r['status'], r['active'], r['5h']['remaining_percent'], r['weekly']['remaining_percent']),
                         ('OK', True, 37.0, 62.0))
        self.assertEqual(r['5h']['reset_at'], '2026-10-05T04:50:00Z')

    def test_live_account_is_never_refreshed_here(self):
        self.enroll('CLAUDE_A', creds('a', expires_in=-60))  # expired: Claude Code refreshes it, not us
        r = self.run_cmd(m.cmd_quota, label=None)['profiles']['CLAUDE_A']
        self.assertEqual(self.refreshes, [])
        self.assertEqual(r['status'], 'OK')  # the API answers or says TOKEN_EXPIRED; we never spend the token

    def test_idle_account_is_refreshed_once_and_the_new_pair_is_saved_at_once(self):
        self.enroll('CLAUDE_B', creds('b', rt='rt-b', expires_in=60))
        self.enroll('CLAUDE_A', creds('a'))  # A is live now, B idle with a nearly expired token
        self.run_cmd(m.cmd_quota, label=None)
        self.assertEqual(self.refreshes, ['rt-b'])
        self.assertEqual(m.decode(self.store['CLAUDE_B'])['oauth']['refreshToken'], 'rt-b-new')
        self.assertIn(('/api/oauth/usage', 'at-b-new'), self.calls)

    def test_idle_account_with_a_valid_token_is_not_refreshed(self):
        self.enroll('CLAUDE_B', creds('b', expires_in=7200))
        self.enroll('CLAUDE_A', creds('a'))
        self.run_cmd(m.cmd_quota, label=None)
        self.assertEqual(self.refreshes, [])

    def test_used_up_window_sets_limit_reached(self):
        self.assertTrue(m.usage_row({'five_hour': {'utilization': 100.0}, 'seven_day': {'utilization': 40.0}})['limit_reached'])


class WarmTests(Base):
    def run_warm(self, label, stdout='CLAUDE_ACCOUNT_SMOKE_OK'):
        seen = {}

        def fake_run(args, **kw):
            seen.update(args=args, env=kw['env'], cwd=kw['cwd'])
            return types.SimpleNamespace(returncode=0, stdout=stdout, stderr='')
        with patch.object(m.subprocess, 'run', side_effect=fake_run):
            try:
                r = self.run_cmd(m.cmd_warm, label=label, model='haiku', timeout=30)
            except SystemExit:
                r = json.loads(self.out.getvalue().strip().splitlines()[-1])
        return r, seen

    def test_warm_uses_a_throwaway_config_and_the_accounts_own_token(self):
        self.enroll('CLAUDE_B', creds('b', expires_in=7200))
        self.enroll('CLAUDE_A', creds('a'))
        r, seen = self.run_warm('CLAUDE_B')
        self.assertEqual(r['status'], 'PASS')
        self.assertEqual(seen['env']['CLAUDE_CODE_OAUTH_TOKEN'], 'at-b')
        self.assertIn('claude-warm-', seen['env']['CLAUDE_CONFIG_DIR'])
        self.assertNotIn('at-b', ' '.join(seen['args']))  # never on the command line

    def test_live_account_warms_with_the_live_token(self):
        self.enroll('CLAUDE_A', creds('a'))
        r, seen = self.run_warm('CLAUDE_A')
        self.assertEqual(seen['env']['CLAUDE_CODE_OAUTH_TOKEN'], 'at-a')
        self.assertEqual(self.refreshes, [])

    def test_quota_wall_is_reported_as_unavailable(self):
        self.enroll('CLAUDE_A', creds('a'))
        r, _ = self.run_warm('CLAUDE_A', stdout="Claude AI usage limit reached|1791000000")
        self.assertEqual(r['status'], 'PROFILE_ACTIVATED_BUT_ACCOUNT_UNAVAILABLE')


class RemoveTests(Base):
    def test_live_account_cannot_be_removed(self):
        self.enroll('CLAUDE_A', creds('a'))
        with self.assertRaises(SystemExit):
            self.run_cmd(m.cmd_remove, label='CLAUDE_A')

    def test_idle_account_is_forgotten(self):
        self.enroll('CLAUDE_B', creds('b'))
        self.enroll('CLAUDE_A', creds('a'))
        self.run_cmd(m.cmd_remove, label='CLAUDE_B')
        self.assertNotIn('CLAUDE_B', self.store)
        self.assertNotIn('CLAUDE_B', m.load_meta()['profiles'])


class LoginTests(Base):
    URL = ('https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e'
           '&response_type=code&scope=user%3Aprofile+user%3Ainference&state=S')

    def setUp(self):
        super().setUp()
        self.screen = 'Opening browser to sign in…\nIf the browser didn\'t open, visit: ' + self.URL + '\nPaste code here if prompted > '
        self.sent, self.private, self.ran = [], None, []
        for name, fake in (('tmux_start', lambda d: None), ('tmux_capture', lambda: self.screen),
                           ('tmux_send', lambda k, literal=False: self.sent.append(k)),
                           ('read_private', lambda d, known=(): dict(self.private, _service='svc-x') if self.private else None),
                           ('private_services', lambda: set())):
            self.stack.enter_context(patch.object(m, name, side_effect=fake))
        self.stack.enter_context(patch.object(m.subprocess, 'run', side_effect=lambda args, **kw: self.ran.append(args)))
        self.stack.enter_context(patch.object(m.time, 'sleep'))
        self.enroll('CLAUDE_A', creds('a'))

    def start(self, label='CLAUDE_B'):
        return self.run_cmd(m.cmd_login_start, label=label)

    def finish(self, code='abcDEF123_-xyz#STATE456'):
        return self.run_cmd(m.cmd_login_finish, code=code)

    def config_dir(self):
        return json.load(open(m.login_state_path()))['config_dir']

    def cleaned(self, config_dir):
        self.assertFalse(Path(m.login_state_path()).exists())
        self.assertFalse(Path(config_dir).exists())
        self.assertTrue(any(a[:2] == ['tmux', 'kill-session'] for a in self.ran))
        self.assertTrue(any(a[1:2] == ['delete-generic-password'] and 'svc-x' in a for a in self.ran))

    def test_start_prints_the_sign_in_url_from_claude(self):
        self.assertEqual(self.start()['url'], self.URL)

    def test_start_refuses_bad_or_taken_labels(self):
        for label in ('claude_b', 'CLAUDE_A', 'CLAUDE_B; rm'):
            with self.assertRaises(SystemExit):
                self.start(label)

    def test_finish_enrolls_the_new_account_and_cleans_up_the_private_login(self):
        self.start()
        d = self.config_dir()
        self.private = creds('b', rt='rt-b')
        r = self.finish()
        self.assertEqual(r['enrolled'], 'CLAUDE_B')
        self.assertIn('abcDEF123_-xyz#STATE456', self.sent)
        self.assertEqual(m.decode(self.store['CLAUDE_B'])['oauth']['refreshToken'], 'rt-b')
        self.assertIn('CLAUDE_B', m.load_meta()['profiles'])
        self.cleaned(d)

    def test_same_account_again_is_refused_and_cleaned_up(self):
        self.start()
        d = self.config_dir()
        self.private = creds('a', rt='rt-again')
        with self.assertRaises(SystemExit) as e:
            self.finish()
        self.assertIn('CLAUDE_A', str(e.exception))
        self.assertNotIn('CLAUDE_B', self.store)
        self.cleaned(d)

    def test_wrong_code_times_out_and_cleans_up(self):
        self.start()
        d = self.config_dir()
        with self.assertRaises(SystemExit):
            self.finish()
        self.assertFalse(Path(d).exists())

    def test_cancel_cleans_up(self):
        self.start()
        d = self.config_dir()
        self.run_cmd(m.cmd_login_cancel)
        self.assertFalse(Path(d).exists())
        self.assertFalse(Path(m.login_state_path()).exists())

    def test_private_keychain_name_follows_claude_code(self):
        self.assertRegex(m.private_service('/x/login-1'), r'^Claude Code-credentials-[0-9a-f]{8}$')

class UseTests(Base):
    def setUp(self):
        super().setUp()
        self.item = {'claudeAiOauth': creds('a')['oauth'], 'mcpOAuth': {'plugin:supabase': {'accessToken': 'mcp'}}}
        self.account = creds('a')['account']
        m.read_live.side_effect = lambda: {'oauth': self.item['claudeAiOauth'], 'account': self.account}
        self.fail_write = False

        def write_item(full):
            if self.fail_write and full['claudeAiOauth']['refreshToken'] != 'rt-a-rotated':
                raise RuntimeError('live keychain write failed')
            self.item = json.loads(json.dumps(full))
        self.stack.enter_context(patch.object(m, 'read_live_item', side_effect=lambda: json.loads(json.dumps(self.item))))
        self.stack.enter_context(patch.object(m, 'write_live_item', side_effect=write_item))
        self.stack.enter_context(patch.object(m, 'write_claude_json_account', side_effect=lambda acc: setattr(self, 'account', acc)))
        self.stack.enter_context(patch.object(m, 'fcntl'))
        self.live = None  # read_live is driven by item/account above
        self.run_cmd(m.cmd_enroll, label='CLAUDE_A')
        self.store['CLAUDE_B'] = m.encode(creds('b', rt='rt-b'))
        meta = m.load_meta()
        meta['profiles']['CLAUDE_B'] = {'id': m.identity(creds('b')), 'enrolled_at': 'x'}
        m.save_meta(meta)
        self.item['claudeAiOauth'] = dict(self.item['claudeAiOauth'], refreshToken='rt-a-rotated')  # Claude Code refreshed

    def use(self, label):
        return self.run_cmd(m.cmd_use, label=label)

    def test_switch_swaps_only_the_claude_login_and_the_account(self):
        r = self.use('CLAUDE_B')
        self.assertEqual((r['selected'], r['previous'], r['activation']), ('CLAUDE_B', 'CLAUDE_A', 'OK'))
        self.assertEqual(self.item['claudeAiOauth']['refreshToken'], 'rt-b')
        self.assertEqual(self.item['mcpOAuth'], {'plugin:supabase': {'accessToken': 'mcp'}})  # MCP logins kept
        self.assertEqual(self.account['accountUuid'], 'b')

    def test_live_tokens_claude_code_rotated_are_saved_back_first(self):
        self.use('CLAUDE_B')
        self.assertEqual(m.decode(self.store['CLAUDE_A'])['oauth']['refreshToken'], 'rt-a-rotated')

    def test_same_account_is_a_no_op(self):
        self.assertEqual(self.use('CLAUDE_A')['activation'], 'UNCHANGED')

    def test_failed_write_rolls_back(self):
        self.fail_write = True
        with self.assertRaises(SystemExit):
            self.use('CLAUDE_B')
        r = json.loads(self.out.getvalue().strip().splitlines()[-1])
        self.assertEqual(r['activation'], 'ROLLED_BACK')
        self.assertEqual(self.item['claudeAiOauth']['refreshToken'], 'rt-a-rotated')
        self.assertEqual(self.account['accountUuid'], 'a')

    def test_unenrolled_live_account_is_never_overwritten(self):
        self.account = creds('stranger')['account']
        with self.assertRaises(SystemExit):
            self.use('CLAUDE_B')
        self.assertEqual(self.item['claudeAiOauth']['refreshToken'], 'rt-a-rotated')

class WriterTests(unittest.TestCase):
    def test_claude_json_keeps_every_other_setting_and_its_mode(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / '.claude.json'
            f.write_text(json.dumps({'oauthAccount': {'accountUuid': 'a'}, 'projects': {'/x': {'allowedTools': ['Bash']}},
                                     'userID': 'u'}))
            f.chmod(0o600)
            with patch.object(m, 'CLAUDE_JSON', str(f)):
                m.write_claude_json_account({'accountUuid': 'b'})
            cfg = json.loads(f.read_text())
            self.assertEqual((cfg['oauthAccount'], cfg['projects'], cfg['userID']),
                             ({'accountUuid': 'b'}, {'/x': {'allowedTools': ['Bash']}}, 'u'))
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_live_item_is_written_hex_encoded_like_claude_code(self):
        full = {'claudeAiOauth': {'accessToken': 'a b"c'}, 'mcpOAuth': {'x': 1}}
        seen = []
        ok = types.SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(m.subprocess, 'run', side_effect=lambda args, **kw: seen.append((args, kw)) or ok):
            m.write_live_item(full)
        args, kw = seen[0]
        payload = kw['input'] if args == [m.SEC, '-i'] else ' '.join(args)
        hexed = payload.split('-X ')[1].strip().strip('"').split('"')[0].split()[0]
        self.assertEqual(json.loads(bytes.fromhex(hexed)), full)
        self.assertNotIn('a b"c', payload)  # the secret never appears in clear text

if __name__ == '__main__':
    unittest.main()
