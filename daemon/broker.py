"""Use the independently usable agy-account switch command."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from quota import Profiles, QuotaError
import paths
import agy_ownership
from agy_ownership import RotationLock
from agy_park import Parker, ParkError

APP = '/Applications/Antigravity.app'
# Helper refused before touching the live Keychain item: safe failures, no recovery needed.
REFUSALS = {b'could not stop': 'CLOSE_TIMEOUT_NOT_SWITCHED', b'agy is running': 'AGY_RUNNING_NOT_SWITCHED',
            b'another account switch': 'SWITCH_BUSY'}
# Why an interactive agy nobody registered blocked a switch (the generic code stays for anything else).
AGY_REFUSALS = {'AGY_WORKING_NOT_SWITCHED', 'AGY_CONVERSATION_PRUNED_NOT_SWITCHED', 'AGY_UNREADABLE_NOT_SWITCHED'}
NOT_SWITCHED = {'ACTIVATION_FAILED_ROLLED_BACK', 'UNOWNED_AGY_NOT_SWITCHED', *AGY_REFUSALS, *REFUSALS.values()}
# Refusals caused by agy the rotator does not own (it never terminates those): retry soon instead of the
# 600 s cooldown, otherwise an exhausted account would stay un-rotated for 10 minutes behind one in-flight job.
# A long unowned production --print job (up to its 180 s timeout) must not be fenced again every ~50 s:
# every refusal that is caused by unowned agy waits >= 120 s.  SWITCH_BUSY (another switch) stays short.
RETRY_AFTER = {'UNOWNED_AGY_NOT_SWITCHED': 120, **{code: 120 for code in AGY_REFUSALS}, 'CLOSE_TIMEOUT_NOT_SWITCHED': 120,
               'AGY_RUNNING_NOT_SWITCHED': 120, 'SWITCH_BUSY': 20}
assert agy_ownership.DRAIN_SECONDS + agy_ownership.SWITCH_BUDGET_SECONDS <= agy_ownership.LOCK_MAX_HOLD_SECONDS


def running():
    pids = set()
    # Orphaned bundle schedulers are not user work; the helper stops them before switching.
    for args in (['-x', 'agy'], ['-f', '^' + APP + '/Contents/MacOS/Antigravity( |$)']):
        r = subprocess.run(['/usr/bin/pgrep', *args], capture_output=True, timeout=5)
        pids.update(int(p) for p in r.stdout.split() if p.isdigit())
    return sorted(pids)


def agy_processes(rows=None):
    """[(pid, pgid, is_print_job)] for every process whose executable is `agy`."""
    if rows is None:
        out = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,command='], capture_output=True, text=True,
                             timeout=5).stdout
        rows = [r for r in (line.split(None, 2) for line in out.splitlines()) if len(r) == 3]
    found = []
    for pid, pgid, cmd in rows:
        argv = cmd.split()
        if argv and os.path.basename(argv[0]) == 'agy' and pid.isdigit() and pgid.isdigit():
            is_print = any(a in PRINT_FLAGS or a.startswith(('--print=', '--prompt=')) for a in argv[1:])
            found.append((int(pid), int(pgid), is_print))
    return found


def unowned_agy(rows=None, owned=None):
    """agy processes outside every live registered process group: the rotator never terminates these."""
    if owned is None:
        owned = {rec['pgid'] for rec in agy_ownership.live_owned()}
    return [(pid, is_print) for pid, pgid, is_print in agy_processes(rows) if pgid not in owned]


CONVERSATIONS = [Path.home() / '.gemini/antigravity-cli/conversations',
                 Path.home() / '.gemini/antigravity/conversations']
RECENT_WRITE = 60  # model turns write every few seconds; long tool calls are caught as children
SPAWN_GRACE = 20   # children started with their parent (MCP servers, schedulers) are not work
PRINT_FLAGS = ('-p', '--print', '--prompt')


def etime_seconds(value):
    days, _, clock = value.rpartition('-')
    parts = [int(x) for x in clock.split(':')]
    hours, minutes, seconds = [0] * (3 - len(parts)) + parts
    return int(days or 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def working(rows=None, now=None, dirs=CONVERSATIONS, ignore_until=0):
    """Work in progress, not mere presence: open-but-quiet agy / app count as idle.
    Conversation writes up to ignore_until are the rotator's own (warm-up, switch probe), not work.
    Returns short reasons (pids only, never argv)."""
    if rows is None:
        out = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,etime=,command='],
                             capture_output=True, text=True, timeout=5).stdout
        rows = [r for r in (line.split(None, 3) for line in out.splitlines()) if len(r) == 4]
    now = time.time() if now is None else now
    procs = {pid: (ppid, etime_seconds(age), cmd) for pid, ppid, age, cmd in rows}
    is_agy = lambda cmd: os.path.basename(cmd.split()[0]) == 'agy'
    is_server = lambda cmd: cmd.startswith(APP + '/Contents/Resources/bin/language_server')
    reasons = []
    for pid, (ppid, age, cmd) in procs.items():
        if is_agy(cmd) and any(a in PRINT_FLAGS or a.startswith(('--print=', '--prompt='))
                               for a in cmd.split()[1:]):
            reasons.append(f'agy_print:{pid}')
        parent = procs.get(ppid)
        if cmd == '<defunct>':  # finished child agy never reaped: not work
            continue
        if (parent and (is_agy(parent[2]) or is_server(parent[2])) and not is_server(cmd)
                and parent[1] - age > SPAWN_GRACE):
            reasons.append(f'child:{pid}')
    for d in dirs:
        try:
            latest = max((f.stat().st_mtime for f in Path(d).glob('*.db')), default=0)
        except OSError:
            continue
        if now - latest < RECENT_WRITE and latest > ignore_until:
            reasons.append('conversation_write')
            break
    return reasons


def parse_warm(proc):
    try:
        result = json.loads(proc.stdout) if proc.stdout else {}
    except ValueError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    if proc.returncode == 0 and result.get('status') == 'PASS':
        return result
    if b'BUSY' in (proc.stderr or b''):
        raise QuotaError('WARM_BUSY')
    if result.get('restore_ok') is False:
        raise QuotaError('WARM_RESTORE_CHECK_CURRENT')
    if result.get('status') == 'PROFILE_ACTIVATED_BUT_ACCOUNT_UNAVAILABLE':
        raise QuotaError('WARM_ACCOUNT_UNAVAILABLE')
    raise QuotaError('WARM_FAILED')


def atomic_json(path, value):
    path = Path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Broker:
    def __init__(self, root, profiles=None, helper=None, parker=None):
        self.profiles = profiles or Profiles()
        self.helper = helper or paths.helper('agy-account')
        self.parker = parker or Parker()
        self.on_park = None  # Service: callback([(item, error)]) after paused agy sessions were resumed

    def warm(self, label):
        """One tiny agy prompt as an idle account (helper restores the live account afterwards)."""
        try:
            proc = subprocess.run([str(self.helper), '--json', 'warm', label], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise QuotaError('WARM_TIMEOUT_CHECK_CURRENT') from None
        return parse_warm(proc)

    def switch(self, label, close_all=False, force=False):
        """force: the live account is used up. Sessions that cannot be paused safely (working, or no matching
        terminal) no longer block the switch; they are left running and may write the used-up account back
        later, which the rotator sees as drift and switches away from again."""
        if label not in self.profiles.list():
            raise QuotaError('UNKNOWN_PROFILE')
        self.profiles.read(label)  # Verify destination before interrupting processes.
        previous = self.profiles.active()
        if previous is None:
            raise QuotaError('LIVE_IDENTITY_UNKNOWN')
        if previous == label:
            return {'previous': previous, 'selected': label, 'activation': 'UNCHANGED'}
        plan = []
        if close_all:
            try:
                blockers = unowned_agy()
            except (OSError, subprocess.SubprocessError, ValueError):
                blockers = []
            interactive = [pid for pid, is_print in blockers if not is_print]
            if interactive:
                # An interactive agy nobody registered would never exit by itself. If it is idle and its
                # conversation is safely saved, quit it (any terminal) and report the command that resumes it;
                # otherwise refuse before taking the lock so new invocations are not held up for a switch that
                # cannot happen.  Unowned --print jobs are bounded: they get the helper's drain window.
                busy = [r for r in working() if not r.startswith('agy_print:')]
                if busy and not force:
                    raise QuotaError('AGY_WORKING_NOT_SWITCHED')  # mid-turn: never quit it
                try:
                    plan = self.parker.plan(interactive)
                except ParkError as e:
                    if not force:
                        # agy keeps only ~500 conversations: one it already pruned would be lost for good if quit
                        # (an agy left open for a day while many `agy -p` jobs run).
                        raise QuotaError('AGY_CONVERSATION_PRUNED_NOT_SWITCHED' if str(e) == 'AGY_CONVERSATION_NOT_SAVED'
                                         else 'AGY_UNREADABLE_NOT_SWITCHED') from None
                except (OSError, ValueError, subprocess.SubprocessError):
                    if not force:
                        raise QuotaError('UNOWNED_AGY_NOT_SWITCHED') from None
                if busy:
                    plan = []  # used up: switch anyway, but still never quit a session mid-turn
        # Short-lived rotation lock (agy_ownership.RotationLock): new agy invocations wait at start only while
        # this transaction runs (acquire -> close owned -> switch -> verify -> release); never held otherwise.
        lock = RotationLock(owner='rotator')
        if not lock.acquire():
            raise QuotaError('SWITCH_BUSY')
        parked = []
        try:
            try:
                self.parker.park(plan, parked)
            except (ParkError, OSError, ValueError, subprocess.SubprocessError):
                raise QuotaError('UNOWNED_AGY_NOT_SWITCHED') from None
            return self._switch_locked(label, previous, close_all, force)
        finally:
            lock.release()
            if parked and self.on_park:  # switched or not: say how to resume each closed session
                self.on_park(self.parker.commands(parked))

    def _switch_locked(self, label, previous, close_all, force=False):
        args = [str(self.helper), '--json', 'use', label]
        if close_all:
            args.append('--close-all')
        if force:
            args.append('--force')
        try:
            proc = subprocess.run(args, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise QuotaError('SWITCH_TIMEOUT_CHECK_CURRENT') from None
        if proc.returncode:
            if proc.returncode == 2:
                try:
                    activation = json.loads(proc.stdout).get('activation')
                except (ValueError, AttributeError):
                    activation = None
                raise QuotaError('ACTIVATION_FAILED_ROLLED_BACK' if activation == 'ROLLED_BACK'
                                 else 'ROLLBACK_FAILED_CHECK_CURRENT')
            error = (proc.stderr or b'').strip().splitlines()[-1:] or [b'']
            code = next((c for k, c in REFUSALS.items() if error[0].startswith(b'ERROR:') and k in error[0]), None)
            raise QuotaError(code or 'SWITCH_FAILED_CHECK_CURRENT')
        try:
            result = json.loads(proc.stdout)
            if result.get('activation') != 'OK' or result.get('selected') != label:
                raise ValueError()
        except (ValueError, AttributeError):
            raise QuotaError('INVALID_SWITCH_RESPONSE') from None
        if self.profiles.active() != label:
            raise QuotaError('LIVE_IDENTITY_CHANGED')
        return {'previous': previous, 'selected': label, 'activation': 'OK',
                'app_reopened': result.get('app_reopened') is True}
