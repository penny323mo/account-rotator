"""Explicit ownership of agy process groups + a short-lived rotation lock.

Why this exists
---------------
The rotator used to run `pkill -TERM -x agy` before an account switch.  That
terminates EVERY agy on the Mac, including production `agy --print` research
that has nothing to do with the dev lane that exhausted the quota.  The rule is
now: the rotator may terminate only process groups that were REGISTERED by
their launcher, are still alive, and whose start time still matches the
registration (guards against PID reuse).  Nothing is matched by command line.

Registry
--------
`<home>/owned/<pgid>.json` = {version, pid, pgid, start, registered_at, launcher}
  * pgid must equal pid: the launcher starts agy as its own process-group leader.
  * `start` is the kernel process start time ("sec.usec", libproc) taken at registration time.
Entries whose process is gone, or whose pid now has a different start time or
group, are STALE and are deleted without signalling anything.

Rotation lock
-------------
`<home>/rotation.lock` is an flock(2) lock plus a JSON body {pid, ts, owner}.
It is held only for the switch transaction (seconds), never during research.
A new agy invocation checks it at START ONLY (`wait_for_rotation`): wait a
bounded time, then the caller proceeds or fails with class ACCOUNT_ROTATION.
Because the kernel drops an flock when its holder dies, a crashed rotator can
never wedge the lock; a live-but-wedged holder is ignored by readers once the
body timestamp is older than `stale_seconds`.

Stdlib only (Python 3.9).  Paths come from env so tests use tmp dirs:
  AGY_ROTATOR_HOME  (default ~/.agy-rotator)
  AGY_OWNED_DIR     (default <home>/owned)
  AGY_ROTATION_LOCK (default <home>/rotation.lock)
"""
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

VERSION = 1
# Lock timing invariant (the production adapter mirrors these numbers):
#   hold = drain window + switch/verify budget <= LOCK_MAX_HOLD_SECONDS
#   readers ignore a holder older than hold + 5 s (stale), so a wedged rotator never blocks research
#   start wait (production, >= 60 s) > LOCK_MAX_HOLD_SECONDS with margin: a start never times out
#   behind a healthy switch.
DRAIN_SECONDS = 20.0          # agy-account close_all wait for unowned --print jobs to exit
SWITCH_BUDGET_SECONDS = 10.0  # keychain swap + read-back verify
LOCK_MAX_HOLD_SECONDS = 40.0
DEFAULT_STALE_SECONDS = LOCK_MAX_HOLD_SECONDS + 5.0
DEFAULT_WAIT_SECONDS = 60.0
assert DRAIN_SECONDS + SWITCH_BUDGET_SECONDS <= LOCK_MAX_HOLD_SECONDS
assert DEFAULT_WAIT_SECONDS >= LOCK_MAX_HOLD_SECONDS + 10.0


def home():
    return Path(os.environ.get('AGY_ROTATOR_HOME') or Path.home() / '.agy-rotator')


def owned_dir():
    return Path(os.environ.get('AGY_OWNED_DIR') or home() / 'owned')


def lock_path():
    return Path(os.environ.get('AGY_ROTATION_LOCK') or home() / 'rotation.lock')


# ---------------------------------------------------------------- process facts
_BSDINFO_SIZE = 136  # sizeof(struct proc_bsdinfo), <sys/proc_info.h>; PROC_PIDTBSDINFO = 3
_SZOMB = 5


def _libproc():
    import ctypes
    import ctypes.util
    path = ctypes.util.find_library('proc')
    return ctypes.CDLL(path) if path else None


def proc_info(pid):
    """(pgid, state, start) or None when the pid does not exist.

    Read through libproc (proc_pidinfo, no fork/exec; `/bin/ps` is setuid and cannot run inside the test
    sandbox).  `start` = "<tv_sec>.<tv_usec>" kernel process start time: exact, so a reused pid never
    matches.  state is 'Z' for a zombie, else 'R'.  Falls back to `ps` if libproc is unusable.
    """
    try:
        import ctypes
        import struct
        lib = _libproc()
        buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
        n = lib.proc_pidinfo(int(pid), 3, 0, buf, _BSDINFO_SIZE)
        if n == _BSDINFO_SIZE:
            status, = struct.unpack_from('<I', buf, 4)
            got_pid, = struct.unpack_from('<I', buf, 12)
            pgid, = struct.unpack_from('<I', buf, 100)
            sec, usec = struct.unpack_from('<QQ', buf, 120)
            if got_pid == int(pid):
                return pgid, ('Z' if status == _SZOMB else 'R'), '%d.%06d' % (sec, usec)
        if n == 0 and ctypes.get_errno() in (0, 3):  # ESRCH: no such process
            return None
    except Exception:
        pass
    try:
        r = subprocess.run(['/bin/ps', '-o', 'pgid=,state=,lstart=', '-p', str(int(pid))],
                           capture_output=True, text=True, timeout=5,
                           env={'LC_ALL': 'C', 'PATH': '/bin:/usr/bin'})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    line = r.stdout.strip()
    parts = line.split(None, 2)
    if r.returncode != 0 or len(parts) != 3 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1], ' '.join(parts[2].split())


# ---------------------------------------------------------------- registry
def _atomic_write(path, text):
    tmp = path.with_name(path.name + '.%d.tmp' % os.getpid())
    with open(tmp, 'w') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def register(pid, launcher, directory=None):
    """Register a freshly spawned process-group leader.  Raises ValueError if it is not one."""
    directory = Path(directory) if directory else owned_dir()
    info = proc_info(pid)
    if info is None:
        raise ValueError('process %s not found' % pid)
    pgid, state, start = info
    if pgid != int(pid):
        raise ValueError('pid %s is not its own process-group leader (pgid %s)' % (pid, pgid))
    directory.mkdir(parents=True, exist_ok=True)
    record = {'version': VERSION, 'pid': int(pid), 'pgid': pgid, 'start': start,
              'registered_at': time.time(), 'launcher': str(launcher)[:80]}
    _atomic_write(directory / ('%d.json' % pgid), json.dumps(record))
    return record


def unregister(pgid, directory=None):
    directory = Path(directory) if directory else owned_dir()
    try:
        (directory / ('%d.json' % int(pgid))).unlink()
        return True
    except (FileNotFoundError, ValueError):
        return False


def _load(path):
    try:
        rec = json.loads(path.read_text())
        if (rec.get('version') == VERSION and isinstance(rec.get('pid'), int)
                and rec.get('pgid') == rec.get('pid') and isinstance(rec.get('start'), str)):
            return rec
    except (OSError, ValueError, AttributeError):
        pass
    return None


def classify(rec, info=proc_info):
    """'live' only if the same process (pid + start time + leader of its group) is still running."""
    cur = info(rec['pid'])
    if cur is None:
        return 'stale'
    pgid, state, start = cur
    if state.startswith('Z'):
        return 'stale'
    if start != rec['start'] or pgid != rec['pgid']:
        return 'reused'
    return 'live'


def entries(directory=None, info=proc_info, clean=True):
    """[(path, record, status)].  With clean=True, stale/reused/malformed files are removed."""
    directory = Path(directory) if directory else owned_dir()
    out = []
    try:
        files = sorted(directory.glob('*.json'))
    except OSError:
        return out
    for p in files:
        rec = _load(p)
        if rec is None or p.stem != str(rec['pgid']):
            status = 'malformed'
        else:
            status = classify(rec, info)
        if clean and status != 'live':
            try:
                p.unlink()
            except OSError:
                pass
        out.append((p, rec, status))
    return out


def live_owned(directory=None, info=proc_info):
    return [rec for _, rec, status in entries(directory, info) if status == 'live']


def terminate_owned(wait=10.0, directory=None, info=proc_info, sleep=time.sleep, clock=time.time):
    """SIGTERM the process group of every live registered entry.  Never SIGKILL, never anything else.

    Returns {'terminated': [pgid...], 'survivors': [pgid...], 'removed_stale': n}.
    """
    directory = Path(directory) if directory else owned_dir()
    found = entries(directory, info)
    removed = sum(1 for _, _, s in found if s != 'live')
    targets = [rec for _, rec, s in found if s == 'live']
    own_group = os.getpgrp()
    signalled = []
    for rec in targets:
        pgid = rec['pgid']
        if pgid <= 1 or pgid == own_group:
            continue
        if classify(rec, info) != 'live':  # re-check immediately before signalling
            continue
        try:
            os.killpg(pgid, signal.SIGTERM)
            signalled.append(pgid)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    deadline = clock() + wait
    pending = list(signalled)
    while pending and clock() < deadline:
        pending = [rec['pgid'] for rec in targets
                   if rec['pgid'] in pending and classify(rec, info) == 'live']
        if pending:
            sleep(0.2)
    for rec in targets:
        if rec['pgid'] not in pending:
            unregister(rec['pgid'], directory)
    return {'terminated': [p for p in signalled if p not in pending],
            'survivors': pending, 'removed_stale': removed}


# ---------------------------------------------------------------- rotation lock
class RotationLock:
    """Held by the rotator for the switch transaction only."""

    def __init__(self, path=None, owner='rotator'):
        self.path = Path(path) if path else lock_path()
        self.owner = owner
        self.fd = None

    def acquire(self, timeout=2.0, poll=0.1):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(poll)
        body = json.dumps({'pid': os.getpid(), 'ts': time.time(), 'owner': self.owner}).encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, body, 0)
        self.fd = fd
        return True

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError('rotation lock busy')
        return self

    def __exit__(self, *exc):
        self.release()


def lock_state(path=None, stale_seconds=DEFAULT_STALE_SECONDS, now=time.time):
    """{'held': bool, 'stale': bool, 'pid', 'age'}.  Never blocks, never takes the lock."""
    path = Path(path) if path else lock_path()
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return {'held': False, 'stale': False, 'pid': None, 'age': None}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return {'held': False, 'stale': False, 'pid': None, 'age': None}
            pid, ts = None, None
            try:
                body = json.loads(os.pread(fd, 4096, 0).decode() or '{}')
                pid, ts = body.get('pid'), float(body.get('ts'))
            except (ValueError, TypeError, OSError):
                try:
                    ts = os.fstat(fd).st_mtime
                except OSError:
                    ts = None
            age = None if ts is None else max(0.0, now() - ts)
            return {'held': True, 'stale': age is not None and age > stale_seconds,
                    'pid': pid, 'age': age}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return {'held': False, 'stale': False, 'pid': None, 'age': None}
    finally:
        os.close(fd)


def wait_for_rotation(path=None, timeout=DEFAULT_WAIT_SECONDS, poll=0.5,
                      stale_seconds=DEFAULT_STALE_SECONDS, sleep=time.sleep, clock=time.monotonic):
    """Start-of-invocation check.  Returns 'FREE', 'WAITED' (free after waiting) or 'TIMEOUT'.

    A stale holder (older than stale_seconds) is treated as free.  Never takes the lock.
    """
    deadline = clock() + timeout
    waited = False
    while True:
        st = lock_state(path, stale_seconds)
        if not st['held'] or st['stale']:
            return 'WAITED' if waited else 'FREE'
        waited = True
        if clock() >= deadline:
            return 'TIMEOUT'
        sleep(poll)


# ---------------------------------------------------------------- CLI (used by the bash launcher)
def _spawn(argv):
    """spawn --launcher NAME --log FILE -- cmd...: start cmd as its own group leader, register, print pid."""
    launcher, log = 'unknown', os.devnull
    while argv and argv[0] != '--':
        flag, val = argv[0], argv[1]
        if flag == '--launcher':
            launcher = val
        elif flag == '--log':
            log = val
        else:
            raise SystemExit('unknown flag ' + flag)
        argv = argv[2:]
    cmd = argv[1:]
    if not cmd:
        raise SystemExit('no command')
    with open(log, 'ab') as sink:
        p = subprocess.Popen(cmd, stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)  # setsid: own session AND own process group
    try:
        register(p.pid, launcher)
    except ValueError as e:
        print('REGISTER_FAILED %s' % e, file=sys.stderr)  # agy keeps running, simply unowned (never killed)
    print(p.pid)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit('usage: agy_ownership.py spawn|wait-lock|status ...')
    cmd, rest = argv[0], argv[1:]
    if cmd == 'spawn':
        return _spawn(rest)
    if cmd == 'wait-lock':
        timeout = float(rest[0]) if rest else DEFAULT_WAIT_SECONDS
        result = wait_for_rotation(timeout=timeout)
        print(result)
        return 3 if result == 'TIMEOUT' else 0
    if cmd == 'status':
        print(json.dumps({'lock': lock_state(), 'owned_live': [r['pgid'] for r in live_owned()]}))
        return 0
    raise SystemExit('unknown command ' + cmd)


if __name__ == '__main__':
    sys.exit(main())
