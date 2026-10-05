"""Close idle interactive agy sessions for a switch, wherever they run, and hand back the command that resumes each.

An agy session reads the live account only when it starts, and an open one writes its own account back into the
Keychain about once an hour, so a switch cannot leave one running.  Instead of refusing, the rotator quits each idle
one (SIGTERM: agy saves the conversation and prints `agy --conversation=<id>` in its own terminal), switches, and
reports per session its folder and the command that continues the same conversation on the new account.

The conversation is identified BEFORE quitting, from the session's own log (agy keeps only its latest 500
conversations: one that was pruned would be lost for good if its process were quit, so such a session is left
alone)."""
import os
import re
import shlex
import signal
import subprocess
import time

EXIT_WAIT = 15  # seconds for agy to save and quit after SIGTERM (2 s observed)
AGY_HOME = os.path.expanduser('~/.gemini/antigravity-cli')
CONVERSATION = re.compile(r'(?:Created conversation|found conversation) ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')


class ParkError(Exception):
    pass


class SystemIO:
    """The real side effects; tests replace this."""

    def conversation(self, pid):
        """(log_found, conversation id or None) from the agy log named after the process start time."""
        out = subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True).stdout
        try:
            started = time.mktime(time.strptime(out.strip(), '%a %b %d %H:%M:%S %Y'))
        except ValueError:
            return False, None
        for delta in range(0, 4):
            path = os.path.join(AGY_HOME, 'log', time.strftime('cli-%Y%m%d_%H%M%S.log', time.localtime(started + delta)))
            if os.path.exists(path):
                with open(path, errors='replace') as f:
                    found = CONVERSATION.findall(f.read())
                return True, (found[-1] if found else None)
        return False, None

    def conversation_saved(self, conversation):
        return os.path.exists(os.path.join(AGY_HOME, 'conversations', conversation + '.db'))

    def argv(self, pid):
        out = subprocess.run(['/bin/ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout
        return shlex.split(out.strip()) if out.strip() else None

    def cwd(self, pid):
        out = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                             capture_output=True, text=True).stdout
        return next((line[1:] for line in out.splitlines() if line.startswith('n')), None)

    def terminate(self, pid):
        os.kill(pid, signal.SIGTERM)

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def sleep(self, seconds):
        time.sleep(seconds)


def resume_command(argv, conversation):
    """The session's own command line, pointed at its conversation (any earlier -c / --conversation dropped)."""
    keep, skip = [], False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ('-c', '--continue'):
            continue
        if arg == '--conversation':
            skip = True
            continue
        if arg.startswith('--conversation='):
            continue
        keep.append(arg)
    return shlex.join(keep + [f'--conversation={conversation}'])


class Parker:
    def __init__(self, io=None):
        self.io = io or SystemIO()

    def plan(self, pids):
        """[{pid, argv, cwd, conversation}] for every pid, or ParkError when any of them cannot be closed safely."""
        items = []
        for pid in pids:
            argv, cwd = self.io.argv(pid), self.io.cwd(pid)
            if not argv or not cwd:
                raise ParkError('AGY_UNREADABLE')
            log_found, conversation = self.io.conversation(pid)
            if not log_found:
                raise ParkError('AGY_LOG_UNKNOWN')
            if conversation and not self.io.conversation_saved(conversation):
                raise ParkError('AGY_CONVERSATION_NOT_SAVED')  # quitting would lose it for good
            items.append({'pid': pid, 'argv': argv, 'cwd': cwd, 'conversation': conversation})
        return items

    def park(self, items, parked):
        """Quit each session. Every stopped item is appended to `parked` at once, so the caller can report it even
        when a later one fails."""
        for item in items:
            self.io.terminate(item['pid'])
            parked.append(item)
            for _ in range(EXIT_WAIT):
                if not self.io.alive(item['pid']):
                    break
                self.io.sleep(1)
            else:
                raise ParkError('AGY_DID_NOT_QUIT')

    @staticmethod
    def commands(parked):
        """Set each closed session's resume command: its own command line on its conversation (a session that never
        got a message had none: it simply starts again)."""
        for item in parked:
            item['command'] = (resume_command(item['argv'], item['conversation']) if item.get('conversation')
                               else shlex.join(item['argv']))
        return parked
