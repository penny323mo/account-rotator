"""Follow a Codex thread that codex-account resumed in tmux: finished, still working, or gone."""
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path


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
                if not (last == 'task_started' or (last == 'task_complete' and not message and age <= quiet_end)):
                    continue
                found.append({'thread': meta['id'], 'cwd': meta.get('cwd'), 'model': self.thread_model(path)})
            except (OSError, ValueError):
                continue
        return found

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

    def release(self, target):
        # Esc first so a pop-up (e.g. "switch model?") is dismissed, never Enter; then Ctrl-C twice to quit.
        for key in ('Escape', 'C-c', 'C-c'):
            subprocess.run(['tmux', 'send-keys', '-t', target, key], capture_output=True)
            time.sleep(1)
