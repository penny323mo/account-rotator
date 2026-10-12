"""Follow a Codex thread that codex-account resumed in tmux: finished, still working, or gone."""
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path


CITATION = re.compile(r'<oai-mem-citation>.*?</oai-mem-citation>', re.S)


def usage_limit(error):
    """A turn's error is the usage limit (a quota switch / wait is the answer), not e.g. the model at capacity."""
    return str(error.get('codex_error_info')) == 'usage_limit_exceeded' or 'usage limit' in str(error.get('message', '')).lower()


def timestamp_of(value):
    from datetime import datetime
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


class WatchIO:
    def __init__(self, codex_home=None, claude_home=None):
        self.codex_home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        self.claude_home = Path(claude_home or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        self._turns = {}  # path -> (bytes parsed, last turn event, final message)

    def rollout(self, thread):
        matches = glob.glob(str(self.codex_home / 'sessions' / '*' / '*' / '*' / f'rollout-*-{thread}.jsonl'))
        return max(matches, key=os.path.getmtime) if matches else None

    def size(self, path):
        return os.path.getsize(path)

    def turn(self, path, start=0):
        """Last turn event of the thread and the final message of its last completed turn, counting only
        what was written from byte `start` on (a resumed thread: not the turn that ended before it).
        Incremental: remembers how far each file was read and only parses appended complete lines,
        so a long turn (hundreds of MB of tool output after task_started) is never misread as idle."""
        size = os.path.getsize(path)
        key = (path, start)
        offset, last, message = self._turns.get(key, (start, None, None))
        if size < offset:  # rewritten / truncated
            offset, last, message = 0, None, None
        if size > offset:
            with open(path, 'rb') as f:
                f.seek(offset)
                for line in f:
                    if not line.endswith(b'\n'):
                        break  # partial line still being written; read it next time
                    offset += len(line)
                    if b'"task_started"' not in line and b'"task_complete"' not in line and b'"turn_aborted"' not in line:
                        continue
                    try:
                        p = json.loads(line).get('payload') or {}
                    except ValueError:
                        continue
                    if p.get('type') in ('task_started', 'task_complete', 'turn_aborted'):
                        last = p['type']
                        message = p.get('last_agent_message') if last == 'task_complete' else None
                        if last == 'task_complete' and p.get('error'):
                            # ended on an error, not finished: the usage limit, or anything else (task_error: e.g.
                            # "Selected model is at capacity", a login that expired)
                            last = 'task_failed' if usage_limit(p['error']) else 'task_error'

        self._turns[key] = (offset, last, message)
        return last, message

    def claude_working(self, max_age=120):
        """Claude Code sessions (any, they all share the live login) that wrote their transcript recently; the
        rotator's own warm-ups use a throwaway config dir and never show up here. Reasons carry ids only."""
        now, found = time.time(), []
        for pattern in ('projects/*/*.jsonl', 'projects/*/*/subagents/*.jsonl'):
            for path in glob.glob(str(self.claude_home / pattern)):
                try:
                    if now - os.path.getmtime(path) <= max_age:
                        found.append('transcript:' + os.path.basename(path)[:-len('.jsonl')][:8])
                except OSError:
                    continue
        return sorted(found)

    def codex_working(self, max_age=1800):
        """Threads (CLI or ChatGPT app) whose latest turn is started but not finished; recent files only."""
        reasons = []
        for path in self.recent_rollouts(max_age):
            try:
                last, _ = self.turn(path)
            except OSError:
                continue
            if last == 'task_started':
                m = re.match(r'rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)\.jsonl$', os.path.basename(path))
                reasons.append(f"turn:{m.group(1) if m else os.path.basename(path)}")
        return reasons

    def recent_rollouts(self, max_age):
        """Rollout files written in the last max_age seconds, whatever day folder they live in: a long-lived
        ChatGPT-app thread keeps appending to the file of the day it started (weeks ago)."""
        now, found = time.time(), []
        for path in glob.glob(str(self.codex_home / 'sessions' / '*' / '*' / '*' / 'rollout-*.jsonl')):
            try:
                if now - os.path.getmtime(path) <= max_age:
                    found.append(path)
            except OSError:
                continue
        return sorted(found)

    def interrupted_threads(self, max_age=1800, quiet_end=600):
        """Main threads worth a "continue" right after a quota switch: a turn still open, or a turn that
        just ended without any reply (how a usage-limit failure looks on disk). Model and cwd included."""
        now, found = time.time(), []
        for path in self.recent_rollouts(max_age):
            try:
                age = now - os.path.getmtime(path)
                with open(path) as f:
                    meta = json.loads(f.readline()).get('payload') or {}
                if meta.get('parent_thread_id') or not meta.get('id'):
                    continue
                last, message = self.turn(path)
                if not (last == 'task_started' or (last in ('task_complete', 'task_failed') and not message and age <= quiet_end)):
                    continue
                found.append({'thread': meta['id'], 'cwd': meta.get('cwd'), 'model': self.thread_model(path)})
            except (OSError, ValueError):
                continue
        return found

    def thread_names(self):
        """{thread id: its title} from Codex's session_index.jsonl (the names ChatGPT lists), latest entry wins."""
        path = self.codex_home / 'session_index.jsonl'
        try:
            mtime = os.path.getmtime(path)
            if getattr(self, '_names', None) and self._names[0] == mtime:
                return self._names[1]
            names = {}
            with open(path, errors='replace') as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get('id') and d.get('thread_name'):
                        names[d['id']] = d['thread_name']
            self._names = (mtime, names)
            return names
        except OSError:
            return {}

    def recent_threads(self, max_age=24 * 3600):
        """Main threads written in the last max_age seconds: {thread, cwd, last turn event, at} for the page's
        session picker (sub-agent threads left out)."""
        found, names = [], self.thread_names()
        for path in self.recent_rollouts(max_age):
            try:
                with open(path) as f:
                    meta = json.loads(f.readline()).get('payload') or {}
                if meta.get('parent_thread_id') or not meta.get('id'):
                    continue
                last = self.turn(path)[0]
                at = self.stopped_at(path) if last in ('task_complete', 'task_failed', 'task_error') \
                    else os.path.getmtime(path)
                found.append({'thread': meta['id'], 'cwd': meta.get('cwd'), 'last': last, 'at': at,
                              'name': names.get(meta['id']),
                              'error': self.turn_error(path) if last == 'task_error' else None})
            except (OSError, ValueError):
                continue
        return found

    def stopped_threads(self, max_age=24 * 3600):
        """Main threads whose last turn ended on the usage limit: what a manual "continue" from the phone is for."""
        found = []
        for path in self.recent_rollouts(max_age):
            try:
                with open(path) as f:
                    meta = json.loads(f.readline()).get('payload') or {}
                if meta.get('parent_thread_id') or not meta.get('id') or self.turn(path)[0] != 'task_failed':
                    continue
                found.append({'thread': meta['id'], 'cwd': meta.get('cwd'), 'model': self.thread_model(path),
                              'at': self.stopped_at(path)})
            except (OSError, ValueError):
                continue
        return found

    def turn_error(self, path):
        """The error message the last turn ended on (its task_complete line), or None."""
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - 256 * 1024))
            lines = f.read().splitlines()
        for line in reversed(lines):
            if b'"task_complete"' in line:
                try:
                    error = (json.loads(line).get('payload') or {}).get('error') or {}
                except ValueError:
                    return None
                return str(error.get('message') or error.get('codex_error_info') or '') or None
        return None

    def stopped_at(self, path):
        """When the last turn ended (its task_complete line), not the file's mtime: ChatGPT writes to a thread just
        by opening it."""
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - 256 * 1024))
            lines = f.read().splitlines()
        for line in reversed(lines):
            if b'"task_complete"' in line:
                try:
                    return timestamp_of(json.loads(line).get('timestamp'))
                except ValueError:
                    break
        return os.path.getmtime(path)

    def last_output(self, thread, tail=4 * 1024 * 1024):
        """(text, timestamp) of the thread's last round: every assistant message (commentary and final) since the last
        user message, oldest first. A round without any (e.g. stopped at once on the limit) gives the one before it."""
        path = self.rollout(thread)
        if not path:
            return None, None
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - tail))
            lines = f.read().splitlines()
        texts, at = [], None
        for line in reversed(lines):
            if b'"message"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get('payload') or {}
            if p.get('type') != 'message':
                continue
            if p.get('role') == 'user' and texts:
                break
            if p.get('role') == 'assistant':
                text = '\n'.join(c.get('text') or '' for c in p.get('content') or [] if isinstance(c, dict))
                text = CITATION.sub('', text).strip()  # Codex's own memory citations are not for reading
                if text:
                    texts.append(text)
                    at = at or d.get('timestamp')
        return ('\n\n'.join(reversed(texts)), at) if texts else (None, None)

    def thread_model(self, path):
        model = None
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - 2 * 1024 * 1024))
            for line in f.read().splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get('payload') or {}
                if d.get('type') == 'turn_context' and p.get('model'):
                    model = p['model']
                elif p.get('type') == 'thread_settings_applied' and (p.get('thread_settings') or {}).get('model'):
                    model = p['thread_settings']['model']
        return model

    def cli_alive(self, thread):
        return subprocess.run(['/usr/bin/pgrep', '-f', f'resume {thread}'], capture_output=True).returncode == 0

    def cli_busy(self, target):
        r = subprocess.run(['tmux', 'capture-pane', '-p', '-t', target], capture_output=True, text=True)
        return r.returncode == 0 and 'Working (' in r.stdout

    def type(self, target, text):
        """Type a prompt into the Codex CLI in that tmux pane (queued after the running turn)."""
        subprocess.run(['tmux', 'send-keys', '-t', target, '-l', text], capture_output=True, timeout=10, check=True)
        subprocess.run(['tmux', 'send-keys', '-t', target, 'Enter'], capture_output=True, timeout=10, check=True)

    def release(self, target):
        # Esc first so a pop-up (e.g. "switch model?") is dismissed, never Enter; then Ctrl-C twice to quit.
        for key in ('Escape', 'C-c', 'C-c'):
            subprocess.run(['tmux', 'send-keys', '-t', target, key], capture_output=True)
            time.sleep(1)
