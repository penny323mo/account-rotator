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

Each session keeps its token in memory and reads the Keychain again only once the modification time of
~/.claude/.credentials.json changed, or its token is about to expire (Claude Code 2.1.289, read from its code on
2026-10-15; until then it kept registering under the old account, and an earlier version of this module waited for
a notice that never came). claude-account use touches that file; the relinker touches it again before typing, and
/remote-control then registers with the new login.

A new bridgeSessionId only counts once the live login can see it (OLD_LOGIN otherwise). Sessions are never restarted:
a session still busy after the time limit, or whose terminal cannot be found, is reported for the user to handle."""
import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

SESSIONS = os.path.expanduser('~/.claude/sessions')
API = 'https://api.anthropic.com/v1/code/sessions/'
TMUX = next((p for p in ('/opt/homebrew/bin/tmux', '/usr/local/bin/tmux', '/usr/bin/tmux') if os.path.exists(p)), 'tmux')
TMUX_PIECE = 2000  # characters; tmux refuses a command over ~16 KB ("command too long"), 2000 x 4-byte chars fits


def type_literal(target, text, tmux=TMUX):
    """Type text into a tmux pane as-is, in pieces so long text (5000+ Chinese characters) is not refused."""
    for i in range(0, len(text), TMUX_PIECE):
        subprocess.run([tmux, 'send-keys', '-t', target, '-l', text[i:i + TMUX_PIECE]], capture_output=True,
                       timeout=10, check=True)
PICKUP = 5           # seconds after touching the credentials file before re-registering
CHANGED = 'account or organization changed'
CREDENTIALS = os.path.expanduser('~/.claude/.credentials.json')
LIMIT = 1800         # give up on sessions still busy after 30 minutes
POLL = 10
MENU = 'Disconnect this session'
SELECTED_DISCONNECT = re.compile(r'❯\s*Disconnect this session')
EMPTY_PROMPT = re.compile(r'^\s*❯[\s\u00a0]*$', re.M)  # the input line with nothing typed (a menu row has text)
PROJECTS = os.path.expanduser('~/.claude/projects')
DIM = re.compile(r'\x1b\[2m.*?(?:\x1b\[(?:0|22)?m|$)', re.M)
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
KEYS = {'up': 'Up', 'enter': 'Enter', 'esc': 'Escape'}


def prompt_typed(content):
    """A user entry that is a prompt someone typed, not a tool result handed back to the model."""
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(isinstance(c, dict) and c.get('type') == 'text' for c in content) \
        and not any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content)


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
        if literal:
            for text in keys:
                type_literal(pane, text)
            return
        subprocess.run([TMUX, 'send-keys', '-t', pane] + list(keys), capture_output=True, timeout=10, check=True)

    def screen(self, pane):
        """The tmux pane's screen as text."""
        return subprocess.run([TMUX, 'capture-pane', '-p', '-t', pane], capture_output=True, text=True,
                              timeout=10, check=True).stdout

    def last_message(self, session_id):
        """(uuid, hit_limit) of the transcript's last user / assistant entry; hit_limit = Claude's own usage-limit
        error ("You've hit your session limit · resets ..."), which is how a stopped session looks on disk."""
        paths = glob.glob(os.path.join(PROJECTS, '*', f'{session_id}.jsonl'))
        if not paths:
            return None, False
        with open(paths[0], 'rb') as f:
            f.seek(max(0, os.path.getsize(paths[0]) - 512 * 1024))
            lines = f.read().splitlines()
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get('type') in ('user', 'assistant'):
                return d.get('uuid'), bool(d.get('isApiErrorMessage')) and d.get('error') == 'rate_limit'
        return None, False

    def last_reply(self, session_id):
        """(uuid, hit_limit) of the transcript's last assistant entry: after "continue" was typed, a new one is the
        session's answer (or the usage limit again)."""
        paths = glob.glob(os.path.join(PROJECTS, '*', f'{session_id}.jsonl'))
        if not paths:
            return None, False
        with open(paths[0], 'rb') as f:
            f.seek(max(0, os.path.getsize(paths[0]) - 512 * 1024))
            lines = f.read().splitlines()
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get('type') == 'assistant':
                return d.get('uuid'), bool(d.get('isApiErrorMessage')) and d.get('error') == 'rate_limit'
        return None, False

    def last_error(self, session_id):
        """{uuid, error, text, timestamp} when the transcript's last assistant entry is an API error other than the
        usage limit (model overloaded, login expired, ...): the session stopped and waits for someone; else None."""
        paths = glob.glob(os.path.join(PROJECTS, '*', f'{session_id}.jsonl'))
        if not paths:
            return None
        with open(paths[0], 'rb') as f:
            f.seek(max(0, os.path.getsize(paths[0]) - 512 * 1024))
            lines = f.read().splitlines()
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get('type') == 'user' and not d.get('isMeta'):
                return None  # someone typed after it (or a tool ran): it moved on
            if d.get('type') != 'assistant':
                continue
            if not d.get('isApiErrorMessage') or d.get('error') == 'rate_limit':
                return None
            text = ' '.join(c.get('text') or '' for c in (d.get('message') or {}).get('content') or []
                            if isinstance(c, dict)).strip()
            return {'uuid': d.get('uuid'), 'error': d.get('error'), 'text': text, 'timestamp': d.get('timestamp')}
        return None

    def last_output(self, session_id, tail=4 * 1024 * 1024):
        """(text, timestamp) of the session's last round: every assistant text since the last prompt the user typed
        (tool results do not count), oldest first, without the usage-limit error. A round without any text gives the
        one before it. (None, None) when there is none."""
        paths = glob.glob(os.path.join(PROJECTS, '*', f'{session_id}.jsonl'))
        if not paths:
            return None, None
        with open(paths[0], 'rb') as f:
            f.seek(max(0, os.path.getsize(paths[0]) - tail))
            lines = f.read().splitlines()
        texts, at = [], None
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            content = (d.get('message') or {}).get('content')
            if d.get('type') == 'user' and texts and not d.get('isMeta') and prompt_typed(content):
                break
            if d.get('type') != 'assistant' or d.get('isApiErrorMessage') or not isinstance(content, list):
                continue
            text = '\n'.join(c.get('text') or '' for c in content if isinstance(c, dict) and c.get('type') == 'text').strip()
            if text:
                texts.append(text)
                at = at or d.get('timestamp')
        return ('\n\n'.join(reversed(texts)), at) if texts else (None, None)

    def stopped_at(self, session_id):
        """When the transcript was last written (the session that stopped last is the one the page offers)."""
        paths = glob.glob(os.path.join(PROJECTS, '*', f'{session_id}.jsonl'))
        return os.path.getmtime(paths[0]) if paths else 0

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

    def notices(self, session_id, tail=2 * 1024 * 1024):
        """[(time, text)] of the Remote Control lines Claude Code wrote into its transcript (its tail), oldest first."""
        paths = glob.glob(os.path.join(PROJECTS, '*', f'{session_id}.jsonl'))
        if not paths:
            return []
        with open(paths[0], 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail))
            data = f.read().decode('utf-8', 'replace')
        out = []
        for line in data.splitlines():
            if 'Remote Control' not in line:
                continue
            try:
                d = json.loads(line)
                at = datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if d.get('type') == 'system' and isinstance(d.get('content'), str) and 'Remote Control' in d['content']:
                out.append((at, d['content']))
        return out

    def poke(self):
        """Touch ~/.claude/.credentials.json (time only, never created): every Claude Code drops its in-memory token."""
        try:
            os.utime(CREDENTIALS)
        except FileNotFoundError:
            pass  # without the file Claude Code reads the Keychain on each check anyway

    def typed_screen(self, pane):
        """The tmux pane's screen without dim text: Claude's greyed-out prompt suggestion is not something typed."""
        out = subprocess.run([TMUX, 'capture-pane', '-p', '-e', '-t', pane], capture_output=True, text=True,
                             timeout=10, check=True).stdout
        return ANSI.sub('', DIM.sub('', out))

    def sleep(self, seconds):
        time.sleep(seconds)

    def each(self, jobs):
        """Run the jobs at the same time and wait for all of them."""
        threads = [threading.Thread(target=job, daemon=True) for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

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

    def dropped_sessions(self):
        """Sessions whose link an earlier switch took away and nobody brought back: {pid: record}. Their last Remote
        Control line is Claude Code's own account-changed notice (one the user disconnected ends otherwise)."""
        out = {}
        for r in self.io.records():
            if r.get('bridgeSessionId') or r.get('kind', 'interactive') != 'interactive' \
                    or not isinstance(r.get('pid'), int) or not r.get('sessionId') or not self.io.alive(r['pid']):
                continue
            try:
                lines = self.io.notices(r['sessionId'])
            except OSError:
                continue
            if lines and CHANGED in lines[-1][1]:
                out[r['pid']] = r
        return out

    def prompt_empty(self, where):
        """Its input line holds nothing: a draft is never sent along with /remote-control."""
        screen = self.io.typed_screen(where)
        return bool(EMPTY_PROMPT.search('\n'.join(screen.rstrip().splitlines()[-14:])))

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

    def stopped_sessions(self):
        """Idle running sessions whose last message is the usage-limit error: {pid: (record, error uuid)}."""
        out = {}
        for r in self.io.records():
            if r.get('status') != 'idle' or not r.get('sessionId') or not isinstance(r.get('pid'), int):
                continue
            try:
                uuid, hit = self.io.last_message(r['sessionId'])
            except (OSError, ValueError):
                continue
            if hit and self.io.alive(r['pid']):
                out[r['pid']] = (r, uuid)
        return out

    def last_reply(self, session_id):
        return self.io.last_reply(session_id)

    def last_error(self, session_id):
        return self.io.last_error(session_id)

    def sessions(self):
        """[(record, hit_limit, last written)] for every running session (the page's session picker)."""
        out = []
        for r in self.io.records():
            if r.get('sessionId') and isinstance(r.get('pid'), int) and self.io.alive(r['pid']):
                try:
                    out.append((r, self.io.last_message(r['sessionId'])[1], self.io.stopped_at(r['sessionId'])))
                except (OSError, ValueError):
                    continue
        return out

    def session(self, session_id):
        """(record, hit_limit) of the running session with this id, or None when it ended."""
        for r in self.io.records():
            if r.get('sessionId') == session_id and isinstance(r.get('pid'), int) and self.io.alive(r['pid']):
                return r, self.io.last_message(session_id)[1]
        return None

    def stopped_at(self, record):
        return self.io.stopped_at(record['sessionId'])

    def last_output(self, record):
        return self.io.last_output(record['sessionId'])

    def resume(self, record, text='continue'):
        """Type "continue" (or the user's own text) into a stopped session. None when typed, else an error code. Only at an empty prompt:
        a draft or a menu (e.g. Claude's own rate-limit options) on screen is left alone and reported."""
        where = self.terminal(record)
        if not where:
            return 'NO_TERMINAL'
        try:
            screen = self.io.typed_screen(where)
            if not EMPTY_PROMPT.search('\n'.join(screen.rstrip().splitlines()[-14:])):
                return 'PROMPT_NOT_READY'
            self.send(where, text=text)
        except (OSError, ValueError, subprocess.SubprocessError):
            return 'TYPE_FAILED'
        return None

    def run(self, report, before=None, stop=lambda: False):
        """Re-register every remote-controlled session; report(record, link_or_None, error_or_None) per session.
        `before`: the sessions noted just before the switch (otherwise they are looked up now); sessions an earlier
        switch left without a link are added. `stop`: true once a newer switch
        started its own run (2026-10-14 12:51-12:56 three runs overlapped, typing into the same sessions)."""
        pending = dict(before) if before is not None else self.remote_sessions()
        try:
            pending.update({pid: r for pid, r in self.dropped_sessions().items() if pid not in pending})
        except (OSError, ValueError):
            pass
        if not pending:
            return
        try:
            self.io.poke()
        except OSError:
            pass
        self.io.sleep(self.pickup)
        started = self.io.now()
        self.io.each([lambda pid=pid: self.relink_one(pid, report, stop, started) for pid in pending])

    def relink_one(self, pid, report, stop, started):
        """One session, alongside the others: tried every poll until it is re-registered or the time limit passes."""
        waiting = 'STILL_BUSY'
        while not stop():
            record = next((r for r in self.io.records() if r.get('pid') == pid), None)
            if record is None or not self.io.alive(pid):
                return  # the session ended
            where = self.terminal(record)
            if not where:
                return report(record, None, 'NO_TERMINAL')
            # a busy session is not waited for: /remote-control runs mid-turn (an "immediate" command)
            try:
                if not self.prompt_empty(where):
                    waiting = 'PROMPT_NOT_READY'
                else:
                    before = record.get('bridgeSessionId')
                    error = self.reregister(where, record.get('name'))
                    if error:
                        return report(record, None, error)
                    after = next((r for r in self.io.records() if r.get('pid') == pid), {}).get('bridgeSessionId')
                    if not after or after == before:
                        return report(record, None, 'NOT_RELINKED')
                    if self.io.owned(after) is False:
                        return report(record, None, 'OLD_LOGIN')  # registered under the previous account
                    return report(record, after, None)
            except (OSError, ValueError, subprocess.SubprocessError):
                # a send that failed is tried again at the next poll
                waiting = 'TYPE_FAILED'
            if self.io.now() - started > self.limit:
                return report(record, None, waiting)
            self.io.sleep(self.poll)
