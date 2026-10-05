"""Claude Code accounts through the independently usable claude-account helper (usage + warm-up)."""
import json
import subprocess
from pathlib import Path
from quota import QuotaError
from broker import parse_warm
import paths

REFUSALS = {b'not enrolled': 'CLAUDE_LIVE_NOT_ENROLLED', b'another account switch': 'SWITCH_BUSY',
            b'missing or malformed': 'CLAUDE_PROFILE_BROKEN', b'unknown profile': 'UNKNOWN_PROFILE',
            b'could not save': 'CLAUDE_SAVE_BACK_FAILED'}


class ClaudeAccounts:
    def __init__(self, helper=None, runner=subprocess.run):
        self.helper = Path(helper or paths.helper('claude-account'))
        self.runner = runner

    def _run(self, *args, timeout):
        if self.runner is subprocess.run and not self.helper.exists():
            raise QuotaError('CLAUDE_NOT_CONFIGURED')
        try:
            return self.runner([str(self.helper), '--json', *args], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise QuotaError('CLAUDE_TIMEOUT') from None
        except OSError:
            raise QuotaError('CLAUDE_NOT_CONFIGURED') from None

    def quota(self):
        proc = self._run('quota', timeout=60)
        try:
            if proc.returncode:
                raise ValueError()
            profiles = json.loads(proc.stdout)['profiles']
        except (ValueError, KeyError, TypeError):
            raise QuotaError('CLAUDE_QUOTA_UNAVAILABLE') from None
        active = next((label for label, r in profiles.items() if r.get('active')), None)
        return {'profiles': profiles, 'active': active}

    def use(self, label):
        """Make an enrolled account the live Claude Code login; running sessions follow within ~30 s."""
        proc = self._run('use', label, timeout=60)
        if proc.returncode == 2:
            try:
                activation = json.loads(proc.stdout).get('activation')
            except (ValueError, AttributeError):
                activation = None
            raise QuotaError('CLAUDE_ACTIVATION_ROLLED_BACK' if activation == 'ROLLED_BACK'
                             else 'CLAUDE_ROLLBACK_FAILED_CHECK_CURRENT')
        if proc.returncode:
            line = (proc.stderr or b'').strip().splitlines()[-1:] or [b'']
            code = next((c for k, c in REFUSALS.items() if line[0].startswith(b'ERROR:') and k in line[0]), None)
            raise QuotaError(code or 'CLAUDE_SWITCH_FAILED')
        try:
            result = json.loads(proc.stdout)
            if result.get('activation') not in ('OK', 'UNCHANGED') or result.get('selected') != label:
                raise ValueError()
        except (ValueError, AttributeError):
            raise QuotaError('CLAUDE_SWITCH_FAILED') from None
        return {'selected': label, 'previous': result.get('previous')}

    def warm(self, label):
        """One tiny `claude -p` with the account's own token in a throwaway config dir (live login untouched)."""
        return parse_warm(self._run('warm', label, timeout=150))
