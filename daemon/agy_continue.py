"""Interactive agy sessions (a tmux pane) that stopped on the usage limit, and typing
"continue" (or the user's own text) into them from the page.

agy shows "You have exhausted your quota on this model." (text found in the agy binary, 2026-10-12) when the
account runs out; a session whose screen bottom shows it and nothing after it is taken as stopped there. Nothing
is typed unless the user presses the page's button, and only into the tmux pane the session itself runs in (agy in
other terminals cannot be typed into and is not listed)."""
import re
import subprocess

from claude_remote import TMUX, type_literal

QUOTA = re.compile(r'exhausted your quota|quota exhausted|resource[_ ]exhausted', re.I)


class SystemIO:
    """The real side effects; tests replace this."""

    def panes(self):
        """[(pane_id, cwd, last output time)] for tmux panes whose foreground program is agy."""
        r = subprocess.run([TMUX, 'list-panes', '-a', '-F',
                            '#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{window_activity}'],
                           capture_output=True, text=True, timeout=10)
        out = []
        for line in r.stdout.splitlines():
            parts = line.split('\t')
            if len(parts) == 4 and parts[1] == 'agy':
                out.append((parts[0], parts[2], float(parts[3]) if parts[3].isdigit() else 0))
        return out

    def screen(self, kind, target):
        return subprocess.run([TMUX, 'capture-pane', '-p', '-t', target], capture_output=True, text=True,
                              timeout=10, check=True).stdout

    def type(self, kind, target, text):
        type_literal(target, text)
        subprocess.run([TMUX, 'send-keys', '-t', target, 'Enter'], capture_output=True, timeout=10, check=True)


class AgyContinue:
    def __init__(self, io=None, lines=25):
        self.io = io or SystemIO()
        self.lines = lines

    def stopped(self):
        """[{kind, target, cwd, at}] for agy sessions whose screen bottom shows the usage-limit message."""
        return [{k: v for k, v in s.items() if k != 'stopped'} for s in self.sessions() if s['stopped']]

    def sessions(self):
        """[{kind, target, cwd, at, stopped}] for every interactive agy session (the page's session picker)."""
        out = []
        for kind, listing in (('tmux', self.io.panes),):
            try:
                found = listing()
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            for target, cwd, at in found:
                try:
                    bottom = [l for l in self.io.screen(kind, target).splitlines() if l.strip()][-self.lines:]
                except (OSError, ValueError, subprocess.SubprocessError):
                    continue
                out.append({'kind': kind, 'target': target, 'cwd': cwd, 'at': at,
                            'stopped': any(QUOTA.search(l) for l in bottom)})
        return out

    def last_output(self, session, lines=60):
        """The bottom of its terminal screen (agy keeps no transcript that is easy to read)."""
        screen = self.io.screen(session['kind'], session['target'])
        return '\n'.join(screen.rstrip().splitlines()[-lines:]).strip() or None

    def alive(self, session):
        """Its terminal still runs agy."""
        try:
            return any(target == session['target'] for target, _, _ in self.io.panes())
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def send(self, session, text='continue', check=True):
        """None when typed, else an error code. check: the screen is read again first, only a session still stopped
        (not for the session already being continued, which the user keeps talking to)."""
        try:
            bottom = [l for l in self.io.screen(session['kind'], session['target']).splitlines() if l.strip()]
            if check and not any(QUOTA.search(l) for l in bottom[-self.lines:]):
                return 'NOT_STOPPED'
            self.io.type(session['kind'], session['target'], text)
        except (OSError, ValueError, subprocess.SubprocessError):
            return 'TYPE_FAILED'
        return None
