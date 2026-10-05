"""Codex accounts through the independently usable codex-account helper (one switch core)."""
import json
import subprocess
from pathlib import Path
from quota import QuotaError
from broker import parse_warm
import paths

REFUSALS = {b'could not stop': 'CLOSE_TIMEOUT_NOT_SWITCHED', b'not enrolled': 'CODEX_LIVE_NOT_ENROLLED', b'another account switch': 'SWITCH_BUSY',
            b'missing or malformed': 'CODEX_PROFILE_BROKEN', b'unknown profile': 'UNKNOWN_PROFILE'}


class CodexAccounts:
    def __init__(self, helper=None, runner=subprocess.run):
        self.helper = Path(helper or paths.helper('codex-account'))
        self.runner = runner

    def _run(self, *args, timeout):
        if self.runner is subprocess.run and not self.helper.exists():
            raise QuotaError('CODEX_NOT_CONFIGURED')
        try:
            return self.runner([str(self.helper), '--json', *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise QuotaError('CODEX_TIMEOUT') from None
        except OSError:
            raise QuotaError('CODEX_NOT_CONFIGURED') from None

    def quota(self):
        proc = self._run('quota', timeout=60)
        try:
            if proc.returncode:
                raise ValueError()
            profiles = json.loads(proc.stdout)['profiles']
        except (ValueError, KeyError, TypeError):
            raise QuotaError('CODEX_QUOTA_UNAVAILABLE') from None
        active = next((label for label, r in profiles.items() if r.get('active')), None)
        return {'profiles': profiles, 'active': active}

    def use(self, label):
        # Same as Gemini's manual switch: quit ChatGPT.app + stop Codex CLI, switch, reopen the app.
        proc = self._run('use', label, '--close-all', timeout=90)
        if proc.returncode == 2:
            try:
                activation = json.loads(proc.stdout).get('activation')
            except (ValueError, AttributeError):
                activation = None
            raise QuotaError('CODEX_ACTIVATION_ROLLED_BACK' if activation == 'ROLLED_BACK'
                             else 'CODEX_ROLLBACK_FAILED_CHECK_CURRENT')
        if proc.returncode:
            line = (proc.stderr or b'').strip().splitlines()[-1:] or [b'']
            code = next((c for k, c in REFUSALS.items() if line[0].startswith(b'ERROR:') and k in line[0]), None)
            raise QuotaError(code or 'CODEX_SWITCH_FAILED')
        try:
            result = json.loads(proc.stdout)
            if result.get('activation') != 'OK' or result.get('selected') != label:
                raise ValueError()
        except (ValueError, AttributeError):
            raise QuotaError('CODEX_SWITCH_FAILED') from None
        return {'selected': label, 'previous': result.get('previous'),
                'codex_running': result.get('codex_running') or [],
                'app_reopened': result.get('app_reopened') is True}

    APP_ID = 'com.openai.codex'

    def app_running(self):
        """ChatGPT desktop (it hosts Codex remote control) is running."""
        return subprocess.run(['/usr/bin/pgrep', '-f', '^/Applications/ChatGPT.app/Contents/MacOS/ChatGPT( |$)'],
                              capture_output=True).returncode == 0

    def open_app(self):
        return subprocess.run(['/usr/bin/open', '-b', self.APP_ID], capture_output=True).returncode == 0

    def warm(self, label):
        """One tiny `codex exec` as an idle account in a throwaway CODEX_HOME (live login untouched)."""
        return parse_warm(self._run('warm', label, timeout=150))

    def continue_thread(self, thread, model=None):
        """Resume a thread in tmux with its own permissions; ChatGPT is closed first, then reopened."""
        args = ['continue', thread, '--close-app'] + (['--model', model] if model else [])
        proc = self._run(*args, timeout=120)
        try:
            if proc.returncode:
                raise ValueError()
            result = json.loads(proc.stdout)
        except (ValueError, TypeError):
            raise QuotaError('CODEX_CONTINUE_FAILED') from None
        return {'tmux': result.get('tmux'), 'watched': result.get('watched') is True}
