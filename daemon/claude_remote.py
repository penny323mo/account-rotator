"""After a Claude account switch, bring Remote Control back in the Claude Code sessions that had it.

A Remote Control link belongs to the account that was live when it was registered, so a switch takes every phone link
offline (the sessions themselves keep working). Each running Claude Code writes ~/.claude/sessions/<pid>.json with
its status (idle / busy), its Remote Control link (bridgeSessionId) and, inside tmux, its pane. Once the new login has
been picked up (Claude Code re-reads the Keychain about every 30 s) and the session is idle, it is re-registered from
its tmux pane (sessions in other terminals cannot be typed into and are reported instead):

  /remote-control on a session that still believes it is connected only opens a menu showing the OLD link, so: /remote-control -> the menu must be on screen -> Up, Up ->
  "Disconnect this session" must be the selected line -> Enter -> /remote-control <its name> again, which registers a
  new link under the live account (without the name the phone lists it as "<hostname>-local-<random words>"). Every key after the first is sent only once the screen shows what it expects (Up at an
  empty prompt would recall the last message); anything unexpected is closed with Escape and reported.

A new bridgeSessionId means it registered again, and only counts once the live login can see it: a session that has
not noticed the switch (some never print "signed-in claude.ai account ... changed") registers under the OLD account,
and only a restart (claude --resume) moves it. Sessions are never restarted: a session still busy after the time
limit, or whose terminal cannot be found, is reported for the user to handle."""
import glob
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

SESSIONS = os.path.expanduser('~/.claude/sessions')
API = 'https://api.anthropic.com/v1/code/sessions/'
TMUX = next((p for p in ('/opt/homebrew/bin/tmux', '/usr/local/bin/tmux', '/usr/bin/tmux') if os.path.exists(p)), 'tmux')
PICKUP = 45          # seconds for running sessions to pick up the new login before re-registering
LIMIT = 1800         # give up on sessions still busy after 30 minutes
POLL = 10
MENU = 'Disconnect this session'
SELECTED_DISCONNECT = re.compile(r'❯\s*Disconnect this session')
KEYS = {'up': 'Up', 'enter': 'Enter', 'esc': 'Escape'}


class SystemIO:
    """The real side effects; tests replace this."""

    def records(self):
        out = []
        for path in glob.glob(os.path.join(SESSIONS, '*.json')):
            try:
                with open(path) as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
        return out

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, TypeError):
            return False

    def tmux(self, pane, *keys, literal=False):
        args = [TMUX, 'send-keys', '-t', pane] + (['-l'] if literal else []) + list(keys)
        subprocess.run(args, capture_output=True, timeout=10, check=True)

    def screen(self, pane):
        """The tmux pane's screen as text."""
        return subprocess.run([TMUX, 'capture-pane', '-p', '-t', pane], capture_output=True, text=True,
                              timeout=10, check=True).stdout

    def owned(self, link):
        """True when the live login can see this Remote Control link, False when it cannot, None when unknown."""
        try:
            raw = subprocess.run(['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-a',
                                  os.environ.get('USER', ''), '-w'], capture_output=True, text=True, timeout=10).stdout
            token = json.loads(raw)['claudeAiOauth']['accessToken']
            req = urllib.request.Request(API + link, headers={
                'Authorization': 'Bearer ' + token, 'anthropic-version': '2023-06-01',
                'anthropic-beta': 'ccr-byoc-2025-07-29'})
            urllib.request.urlopen(req, timeout=20).close()
            return True
        except urllib.error.HTTPError as e:
            return False if e.code == 404 else None
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return None

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        return time.time()


class Relinker:
    def __init__(self, io=None, pickup=PICKUP, limit=LIMIT, poll=POLL):
        self.io = io or SystemIO()
        self.pickup, self.limit, self.poll = pickup, limit, poll

    def remote_sessions(self):
        """Running Claude Code sessions that have a Remote Control link: {pid: record}."""
        return {r['pid']: r for r in self.io.records()
                if r.get('bridgeSessionId') and isinstance(r.get('pid'), int) and self.io.alive(r['pid'])}

    def terminal(self, record):
        """The session's tmux pane, or None outside tmux."""
        if record.get('tmux'):
            return record['tmux'].split(':', 1)[-1].split('.', 1)[-1]  # 'claude:@0.%0' -> '%0'
        return None

    def send(self, where, key=None, text=None):
        if text:
            self.io.tmux(where, text, literal=True)
        self.io.tmux(where, KEYS[key or 'enter'])

    def bottom(self, where, lines=14):
        return '\n'.join(self.io.screen(where).rstrip().splitlines()[-lines:])

    def reregister(self, where, name=None):
        """None when done, else an error code (the screen was not what the next key expects).
        The name goes on every /remote-control: a connected session ignores it and opens the menu, a disconnected one
        (Claude Code drops the link itself when it notices the account change) registers under it."""
        name = ' '.join((name or '').split())
        command = f'/remote-control {name}' if name else '/remote-control'
        self.send(where, text=command)
        self.io.sleep(3)
        if MENU in self.bottom(where):  # it still holds a link (the old account's): disconnect it first
            self.send(where, 'up')
            self.send(where, 'up')
            self.io.sleep(1)
            if not SELECTED_DISCONNECT.search(self.bottom(where)):
                self.send(where, 'esc')
                return 'MENU_UNEXPECTED'
            self.send(where, 'enter')
            self.io.sleep(3)
            if MENU in self.bottom(where):
                self.send(where, 'esc')
                return 'MENU_UNEXPECTED'
            self.send(where, text=command)
        self.io.sleep(8)
        if MENU in self.bottom(where):  # connected already (nothing to disconnect after all): just close the menu
            self.send(where, 'esc')
        return None

    def run(self, report, before=None):
        """Re-register every remote-controlled session; report(record, link_or_None, error_or_None) per session.
        `before`: the sessions noted just before the switch (otherwise they are looked up now)."""
        pending = dict(before) if before is not None else self.remote_sessions()
        if not pending:
            return
        self.io.sleep(self.pickup)
        started = self.io.now()
        while pending:
            for pid in list(pending):
                record = next((r for r in self.io.records() if r.get('pid') == pid), None)
                if record is None or not self.io.alive(pid):
                    pending.pop(pid)  # the session ended
                    continue
                if record.get('status') != 'idle':
                    continue
                before = pending.pop(pid).get('bridgeSessionId')
                where = self.terminal(record)
                if not where:
                    report(record, None, 'NO_TERMINAL')
                    continue
                try:
                    error = self.reregister(where, record.get('name'))
                except (OSError, ValueError, subprocess.SubprocessError):
                    error = 'TYPE_FAILED'
                if error:
                    report(record, None, error)
                    continue
                after = next((r for r in self.io.records() if r.get('pid') == pid), {}).get('bridgeSessionId')
                if not after or after == before:
                    report(record, None, 'NOT_RELINKED')
                elif self.io.owned(after) is False:
                    report(record, None, 'OLD_LOGIN')  # registered under the previous account
                else:
                    report(record, after, None)
            if pending and self.io.now() - started > self.limit:
                for record in pending.values():
                    report(record, None, 'STILL_BUSY')
                return
            if pending:
                self.io.sleep(self.poll)
