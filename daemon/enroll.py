"""Add accounts from the web console through the account helpers (phone-friendly sign-in)."""
import json
import subprocess
from pathlib import Path
from quota import QuotaError
import paths

HELPERS = {'gemini': paths.helper('agy-account'), 'codex': paths.helper('codex-account'),
           'claude': paths.helper('claude-account')}
FAILURES = {b'already enrolled': 'ALREADY_ENROLLED', b'did not complete': 'SIGN_IN_NOT_COMPLETED',
            b'already in progress': 'ENROLLMENT_IN_PROGRESS', b'could not stop': 'CLOSE_TIMEOUT_NOT_ADDED',
            b'does not look like': 'INVALID_AUTH_CODE', b'not enrolled': 'LIVE_NOT_ENROLLED',
            b'did not show a sign-in URL': 'AGY_NO_SIGNIN_URL', b'unexpected agy login menu': 'AGY_LOGIN_SCREEN_CHANGED',
            b'is the live account': 'ACCOUNT_IN_USE', b'unknown profile': 'UNKNOWN_PROFILE'}


class AccountAdder:
    def __init__(self, helpers=None, runner=subprocess.run):
        self.helpers = helpers or HELPERS
        self.runner = runner

    def _run(self, provider, args, timeout, fallback):
        try:
            proc = self.runner([str(self.helpers[provider]), '--json', *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise QuotaError(fallback + '_TIMEOUT') from None
        if proc.returncode:
            line = (proc.stderr or b'').strip().splitlines()[-1:] or [b'']
            raise QuotaError(next((c for k, c in FAILURES.items() if k in line[0]), fallback))
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise QuotaError(fallback) from None

    def start(self, provider, label):
        return self._run(provider, ['login-start', label], 150 if provider == 'gemini' else 60, 'ADD_START_FAILED')

    def finish(self, provider, code):
        return self._run(provider, ['login-finish', '--code', code], 180, 'ADD_FINISH_FAILED')

    def status(self, provider):
        return self._run(provider, ['login-status'], 30, 'ADD_STATUS_FAILED')

    def remove(self, provider, label):
        return self._run(provider, ['remove', label], 30, 'REMOVE_FAILED')

    def cancel(self, provider):
        return self._run(provider, ['login-cancel'], 60, 'ADD_CANCEL_FAILED')
