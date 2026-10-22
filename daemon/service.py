"""Local JSON RPC service, independent quota polling and guarded auto rotation."""
import copy
import fcntl
import json
import os
import shlex
import socket
import socketserver
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from broker import NOT_SWITCHED, RETRY_AFTER, Broker, atomic_json, working
from codex import CodexAccounts
from codex_watch import WatchIO, timestamp_of
from claude import ClaudeAccounts
from claude_remote import PICKUP, Relinker
from agy_continue import AgyContinue
import gemini_app
from enroll import AccountAdder
import re
from quota import Profiles, QuotaClient, QuotaError, stamp, timestamp

DEFAULTS = {'enabled': False, 'poll_seconds': 60, 'time_enabled': False,
            'hourly_minutes': [15], 'usage_enabled': True, 'remaining_below': 5,
            'family': 'gemini', 'cooldown_seconds': 600, 'mode': 'auto',
            'windows': ['5h', 'weekly'], 'participants': [], 'countdown_seconds': 60,
            'manual_confirm': True, 'close_app': True, 'notify': True}


# Gemini order (priority_order): quota expiring within 48 h first, then most weekly quota left, then earliest
# weekly reset, then earliest 5 h reset. A live account whose 5 h window refills within 15 min is kept instead of interrupting work.
# (Codex uses the same order through codex_order, but only switches when the live account is used up.)
# Keep every account in the pool to cover 5 h gaps: an account whose weekly quota falls under 15 % is kept
# as a backup (switched away from when idle while a normal account is available). Its last ~10 %+ (about
# three hours of work) is used only when every normal account has hit its 5 h cap; after that it may run out.
PRIORITY_HEADROOM = 15
BACKUP_FLOOR = 10
EXPIRING_SOON = 48 * 3600
URGENT_RESET = 6 * 3600  # weekly quota resetting within 6 h is used first, even below 20 %
URGENT_FLOOR = 1  # % weekly: below this there is nothing left to burn
RESET_BUCKET = 6 * 3600
STAY_WINDOW = 15 * 60


# Warm-up: a 5 h window only starts counting at first use, so an idle account that refilled to 100 % gets one
# tiny prompt a minute later; its clock then runs while it waits instead of starting when it is needed.
WARM_DELAY = 60
CLAUDE_MAX_INTERVAL = 600  # Anthropic rate-limits /api/oauth/usage (shared with every Claude Code session): after
                           # an HTTP 429 poll half as often, up to this, and ease back after 10 good polls
WARM_RETRY = 30 * 60
FIVE_HOURS = 5 * 3600
WARM_SLACK = 180  # reset still ~5 h away (moving with "now") = clock not running


def clock_idle(remaining, reset, now):
    return (isinstance(remaining, (int, float)) and remaining >= 100 and bool(reset)
            and reset - now >= FIVE_HOURS - WARM_SLACK)


def claude_transient(status):
    return status in ('HTTP_429', 'NETWORK_ERROR') or bool(re.fullmatch(r'HTTP_5\d\d', status or ''))


def claude_clock_idle(remaining, reset, now):
    """Claude reports no reset time while a 5 h window has not started; a full window resetting ~5 h out
    has not started counting either."""
    return isinstance(remaining, (int, float)) and remaining >= 100 and (not reset or reset - now >= FIVE_HOURS - WARM_SLACK)


def reset_epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    return timestamp(value) or None


def edf_order(items, now):
    """items: (usable, weekly_reset, payload). Earliest weekly reset first; anything resetting within
    RESET_BUCKET of the earliest counts as a tie and the most usable of those goes first."""
    far = now + 10 * 365 * 86400
    keyed = sorted(((r if r and r > now else far), -u, u, p) for u, r, p in items)
    if not keyed:
        return []
    ties = [k for k in keyed if k[0] <= keyed[0][0] + RESET_BUCKET]
    best = min(ties, key=lambda k: k[1])
    return [best[3]] + [k[3] for k in keyed if k is not best]


def priority_order(items, now):
    """items: (weekly, weekly_reset, five_reset, payload). Quota that expires within 48 h (and is above the
    15 % floor) goes first; then every account by earliest weekly reset (what is lost first is spent first),
    then the earliest 5 h reset (a running window refills sooner than an unstarted one). A fresh account is not
    preferred: the 5 h warm-up starts its weekly clock as well."""
    far = now + 10 * 365 * 86400
    at = lambda t: t if t and t > now else far
    expiring = lambda w, r: w >= PRIORITY_HEADROOM and at(r) - now <= EXPIRING_SOON
    return [p for *_, p in sorted(((not expiring(w, r), at(r), at(f), p) for w, r, f, p in items),
                                  key=lambda x: x[:3])]


def urgent(weekly, weekly_reset, now):
    """Weekly quota that is lost within URGENT_RESET unless used now."""
    return (isinstance(weekly, (int, float)) and weekly >= URGENT_FLOOR
            and bool(weekly_reset) and 0 < weekly_reset - now <= URGENT_RESET)


def codex_order(rows, active, now, floor):
    """Switch targets for Codex, best first: the same order as Antigravity. Quota lost within 6 h goes first
    (earliest reset first), then quota expiring within 48 h, then the earliest weekly reset, then the earliest
    5 h reset. Accounts under 15 % weekly, or with a 5 h window under `floor` %, come last. Only accounts with
    something left in both windows qualify at all."""
    first, normal, backup = [], [], []
    for label, r in rows.items():
        if label == active or not codex_available(r):
            continue
        five, weekly = r.get('5h') or {}, r.get('weekly') or {}
        w, wr, fr = weekly.get('remaining_percent'), reset_epoch(weekly.get('reset_at')), reset_epoch(five.get('reset_at'))
        if five.get('remaining_percent', 0) < floor:
            backup.append((w, wr, fr, label))  # a 5 h window this empty cannot carry work: last resort
        elif urgent(w, wr, now):
            first.append((wr, label))
        elif w >= PRIORITY_HEADROOM:
            normal.append((w, wr, fr, label))
        else:
            backup.append((w, wr, fr, label))
    return [label for _, label in sorted(first)] + priority_order(normal, now) + priority_order(backup, now)


def gemini_windows(row, family):
    group = next((g for g in (row or {}).get('groups', []) if g.get('family') == family), None)
    if not group:
        return None
    w = group.get('windows') or {}
    return {k: ((w.get(k) or {}).get('remaining_percent'), reset_epoch((w.get(k) or {}).get('reset_at')))
            for k in ('5h', 'weekly')}


def why(label, usable, weekly, weekly_reset, now):
    when = f"，{(weekly_reset - now) / 3600:.0f} 小時後 reset" if weekly_reset and weekly_reset > now else ""
    return f"{label}：可用 {usable:.0f}%，每週剩 {weekly:.0f}%{when}"


CODEX_COOLDOWN = 600  # seconds between Codex switches, so stale numbers cannot ping-pong accounts
CLAUDE_CONTINUE_GAP = 15 * 60  # a session that stops on the limit again is retried no sooner than this
CLAUDE_CONTINUE_TRIES = 3      # ... and at most this often until it gets past the limit
CLAUDE_REPLY_WAIT = 30 * 60    # "continue" typed but no answer after this long: reported
ERROR_RETRY_AFTER = 5 * 60     # a session stopped on an error while its account still had quota gets "continue" this
ERROR_RETRY_TRIES = 3          # long after it stopped (same session, live account), at most this many times in a row


def transient(error):
    """Network / rate-limit / server-side errors that usually clear on the next poll (not auth problems)."""
    return (error in ('NETWORK_UNAVAILABLE', 'INVALID_RESPONSE', 'RESPONSE_TOO_LARGE', 'QUOTA_INTERNAL_ERROR')
            or error.endswith(('_HTTP_429', '_HTTP_500', '_HTTP_502', '_HTTP_503', '_HTTP_504')))


CONTINUE_TEXT_MAX = 50000  # characters; Chinese is 3 bytes each, the request limits below allow it


def continue_text(value):
    """What the page's continue button sends: "continue" unless the user typed something else (one line)."""
    if value is None:
        return 'continue'
    if not isinstance(value, str):
        raise QuotaError('INVALID_TEXT')
    text = ' '.join(value.split())
    if not text or len(text) > CONTINUE_TEXT_MAX:
        raise QuotaError('INVALID_TEXT')
    return text


def codex_exhausted(r):
    return r.get('status') == 'OK' and (bool(r.get('limit_reached')) or any(
        (r.get(w) or {}).get('remaining_percent', 0) <= 0 for w in ('5h', 'weekly')))


def gemini_spent(row, family):
    """Fresh data says 0 % left in the 5 h or weekly window: not a switch target."""
    wins = gemini_windows(row, family) if row and row.get('status') == 'OK' else None
    return bool(wins) and any(isinstance(v, (int, float)) and v <= 0 for v, _ in wins.values())


def codex_available(r):
    return r.get('status') == 'OK' and not r.get('limit_reached') and all(
        (r.get(w) or {}).get('remaining_percent', 0) > 0 for w in ('5h', 'weekly'))


def notify_macos(message):
    def deliver():
        try:
            script = 'display notification ' + json.dumps(message, ensure_ascii=False) + ' with title "AGY Rotator"'
            subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass  # Event remains visible through status even if OS notifications are denied.
    threading.Thread(target=deliver, daemon=True).start()


def validate(config):
    if set(config) != set(DEFAULTS):
        raise QuotaError('INVALID_CONFIG_FIELDS')
    for key in ('enabled', 'time_enabled', 'usage_enabled', 'manual_confirm', 'close_app', 'notify'):
        if type(config[key]) is not bool:
            raise QuotaError('INVALID_CONFIG_BOOLEAN')
    for key, low, high in [('poll_seconds', 30, 3600), ('remaining_below', 1, 100),
                           ('cooldown_seconds', 60, 86400), ('countdown_seconds', 10, 600)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise QuotaError('INVALID_CONFIG_RANGE')
    minutes = config['hourly_minutes']
    if (not isinstance(minutes, list) or not minutes or len(minutes) > 60 or
        any(type(m) is not int or not 0 <= m < 60 for m in minutes) or
        len(minutes) != len(set(minutes))):
        raise QuotaError('INVALID_HOURLY_MINUTES')
    if config['family'] not in ('gemini', 'claude_gpt'):
        raise QuotaError('INVALID_MODEL_FAMILY')
    if config['mode'] not in ('auto', 'notify'):
        raise QuotaError('INVALID_MODE')
    if (not isinstance(config['windows'], list) or not config['windows'] or
        any(w not in ('5h', 'weekly') for w in config['windows']) or
        len(set(config['windows'])) != len(config['windows'])):
        raise QuotaError('INVALID_WINDOWS')
    if (not isinstance(config['participants'], list) or
        any(not isinstance(p, str) for p in config['participants']) or
        len(set(config['participants'])) != len(config['participants'])):
        raise QuotaError('INVALID_PARTICIPANTS')
    return copy.deepcopy(config)


def metrics(row, config, now):
    if not row or row.get('status') != 'OK':
        return None
    age = now - timestamp(row.get('updated_at'))
    if age < 0 or age > max(120, config['poll_seconds'] * 2):
        return None
    group = next((g for g in row.get('groups', []) if g['family'] == config['family']), None)
    if not group:
        return None
    # a window Google does not report has no limit (since 2026-10-22 some accounts get the weekly one only)
    values = [group['windows'][w].get('remaining_percent') for w in config['windows'] if w in group['windows']]
    return values if values and all(type(v) in (int, float) and 0 <= v <= 100 for v in values) else None


class Service:
    def __init__(self, root, profiles=None, client=None, broker=None, busy=None, notifier=None, codex=None, watch_io=None, adder=None, claude=None,
                 relinker=None, agy_continue=None, gemini_app_=None):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.lock = threading.RLock()
        self.switch_lock = threading.Lock()
        self.profiles = profiles or Profiles()
        self.client = client or QuotaClient(self.profiles)
        self.broker = broker or Broker(self.root, self.profiles)
        self.broker.on_park = self.agy_closed
        self.own_agy_until = 0.0  # end of the rotator's own last agy run (warm-up / switch probe)
        self.busy = busy or (lambda: working(ignore_until=self.own_agy_until))
        self.codex = codex or CodexAccounts()
        self.codex_state = {'profiles': {}, 'active': None, 'updated_at': None, 'error': None, 'switching': False,
                            'auto': False, 'autocontinue': False, 'waitcontinue': True, 'stopped': []}
        self.codex_manual_continuing = False
        self.codex_lock = threading.Lock()
        self.watch_io = watch_io or WatchIO()
        self.claude = claude or ClaudeAccounts()
        self.relinker = relinker or Relinker()
        self.gemini_app = gemini_app_ or gemini_app.GeminiApp(gemini_app.SystemIO(names=self.root / 'gemini_app.json'))
        self.gemini_app_lock = threading.Lock()
        self.claude_target = None  # {'sessionId', 'at'}: the session ▶ keeps talking to
        self.agy_target = None     # the agy session ▶ keeps talking to
        try:  # kept across restarts
            saved = json.loads((self.root / 'continue_targets.json').read_text())
            self.claude_target, self.agy_target = saved.get('claude'), saved.get('agy')
        except (OSError, ValueError, AttributeError):
            pass
        self.agy_continue = agy_continue or AgyContinue()
        self.agy_stopped = []  # interactive agy sessions stopped on the usage limit (the page's ▶ button)
        self.agy_continuing = False
        self.claude_state = {'profiles': {}, 'active': None, 'updated_at': None, 'error': None, 'switching': False,
                             'waitcontinue': True, 'stopped': []}
        try:
            self.claude_state['waitcontinue'] = json.loads((self.root / 'claude.json').read_text()).get('waitcontinue') is not False
        except (OSError, ValueError, AttributeError):
            pass
        self.claude_waiting = False
        self.claude_relinking = False
        self.claude_relink_gen = 0  # a newer switch's relink supersedes the one still running
        self.claude_continued = {}  # session id -> {'uuid', 'tries', 'at'}: what was already continued
        self.claude_continuing = False
        self.claude_lock = threading.Lock()
        self.claude_next_poll = 0.0
        self.claude_interval, self.claude_good = 0, 0  # 0 = the normal poll_seconds
        self.refresh_scope = None
        self.adder = adder or AccountAdder()
        self.enrolling = None  # {'provider','label','stage','url','code','started_at'} while adding an account
        try:
            self.codex_watches = json.loads((self.root / 'codex_watches.json').read_text())
            if not isinstance(self.codex_watches, list):
                self.codex_watches = []
        except (OSError, ValueError):
            self.codex_watches = []
        self.last_codex_switch = 0
        self.codex_all_exhausted = False
        try:  # threads stopped while every Codex account was used up; continued once quota returns
            self.codex_waiting = json.loads((self.root / 'codex_waiting.json').read_text())
            if not isinstance(self.codex_waiting, list):
                self.codex_waiting = []
        except (OSError, ValueError):
            self.codex_waiting = []
        self.codex_all_exhausted = bool(self.codex_waiting)  # already reported before a restart
        try:  # sessions already reported as stopped on an error: {key: when}, so a restart does not report them again
            self.session_errors = json.loads((self.root / 'session_errors.json').read_text())
            if not isinstance(self.session_errors, dict):
                self.session_errors = {}
        except (OSError, ValueError):
            self.session_errors = {}
        self.error_retries = {}  # 'codex:<thread>' / 'claude:<session>' -> {'stop': stop retried last, 'tries': n}
        # Is the live account busy? None = could not tell (never shown as idle).
        self.activity = {k: {'working': None, 'reasons': [], 'checked_at': None} for k in ('gemini', 'codex', 'claude')}
        try:
            saved = json.loads((self.root / 'codex.json').read_text())
            self.codex_state['auto'] = saved.get('auto') is True
            self.codex_state['autocontinue'] = saved.get('autocontinue') is True
            self.codex_state['waitcontinue'] = saved.get('waitcontinue') is not False
        except (OSError, ValueError, AttributeError):
            pass
        self.warmup = {'gemini': True, 'codex': True, 'claude': True}
        try:
            saved = json.loads((self.root / 'warmup.json').read_text())
            self.warmup.update({k: saved[k] for k in self.warmup if type(saved.get(k)) is bool})
        except (OSError, ValueError, AttributeError):
            pass
        self.warm_seen, self.warm_last = {}, {}
        self.notifier = notifier or (lambda message: None)
        self.config_path = self.root / 'config.json'
        self.config = copy.deepcopy(DEFAULTS)
        try:
            initial_labels = list(self.profiles.list())
        except QuotaError:
            initial_labels = []
        self.config['participants'] = initial_labels
        self.config_error = None
        try:
            saved = json.loads(self.config_path.read_text())
            self.config = validate({**self.config, **saved})
            if set(saved) != set(self.config):
                atomic_json(self.config_path, self.config)
        except FileNotFoundError:
            atomic_json(self.config_path, self.config)
        except Exception:
            # Preserve malformed settings for recovery. Never silently enable.
            self.config = {**copy.deepcopy(DEFAULTS), 'participants': initial_labels}
            self.config_error = 'INVALID_SAVED_CONFIG_AUTOMATION_DISABLED'
        self.revision = time.time_ns()
        self.rows = {label: {'label': label, 'status': 'UNAVAILABLE', 'groups': []}
                     for label in initial_labels}
        self.events = []
        self.pending = None
        self.active = None
        self.active_checked_at = None
        self.refreshing = False
        self.refresh_requested = threading.Event()
        self.stop = threading.Event()
        self.last_switch = 0
        self.last_slot = None
        self.switching = False
        self.last_auto_attempt = 0
        self.cancelled_until = 0
        self.epoch = str(time.time_ns())
        self.persistence_error = None
        self.paused_until = 0
        self.transaction = None
        self.recovery_required = False
        self.last_refresh_finished = None
        try:
            state = json.loads((self.root / 'state.json').read_text())
            self.last_switch = float(state.get('last_switch', 0))
            self.last_slot = state.get('last_slot')
            self.cancelled_until = float(state.get('cancelled_until', 0))
            if isinstance(state.get('events'), list):
                self.events = state['events'][-50:]
            self.paused_until = float(state.get('paused_until', 0))
            self.transaction = state.get('transaction')
            self.recovery_required = (isinstance(self.transaction, dict) and
                                      self.transaction.get('phase') in ('STARTED', 'UNKNOWN'))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self.recovery_required = True
            self.transaction = {'phase': 'UNKNOWN', 'error': 'SAVED_STATE_UNREADABLE'}

    def autocontinue_event(self, project, selected, kind='codex_autocontinue'):
        """The continue script already logged codex_continue_started for this thread: one line per continue."""
        last = self.events[-1] if self.events else {}
        if last.get('kind') == 'codex_continue_started' and last.get('project') == project:
            self.events.pop()
        self.event(kind, project=project, selected=selected)

    def event(self, kind, **details):
        item = {'time': stamp(), 'kind': kind, **details}
        self.events = (self.events + [item])[-100:]
        self.persist()
        messages = {'rotation_recommended': '已達輪轉條件，只通知模式未切換。',
                    'countdown_started': 'Quota 已用盡，倒數後將關閉 agy 並切換。可在 App 取消。',
                    'park_countdown_started': '每週額度低過 15%，倒數後轉去其他帳號，留低呢個做後備。可在 App 取消。',
                    'switched': '帳號切換完成。', 'switch_failed': '帳號切換失敗，請查看 App 狀態。',
                    'codex_all_exhausted': 'Codex 所有帳號都已用盡，未有自動切換。',
                    'codex_waiting_quota': 'Codex 用緊嘅帳號用盡：佢恢復額度就會自動接續停咗嘅對話。',
                    'claude_waiting_quota': 'Claude 用緊嘅帳號撞到用量上限：額度恢復就會自動喺停咗嘅 session 打 continue。',
                    'codex_switch_failed': 'Codex 帳號切換失敗，請查看 App 狀態。'}
        if self.config['notify'] and kind in messages:
            self.notifier(messages[kind])

    def drop_auto_pending(self):
        # A queued manual switch is the user's explicit request; only cancel removes it.
        if not (self.pending and self.pending.get('reason') == 'manual'):
            self.pending = None

    def persist(self):
        try:
            atomic_json(self.root / 'state.json', {'last_switch': self.last_switch,
                                                  'last_slot': self.last_slot,
                                                  'cancelled_until': self.cancelled_until,
                                                  'paused_until': self.paused_until,
                                                  'transaction': self.transaction,
                                                  'events': self.events})
        except OSError:
            self.persistence_error = 'STATE_WRITE_FAILED_AUTOMATION_DISABLED'
            self.config['enabled'] = False
            self.pending = None

    def configure(self, params):
        with self.lock:
            if params.get('revision') != self.revision:
                raise QuotaError('CONFIG_CONFLICT_RELOAD_REQUIRED')
            new = validate(params.get('config', {}))
            known = self.profiles.list()
            if not new['participants'] or any(label not in known for label in new['participants']):
                raise QuotaError('SELECT_AT_LEAST_ONE_KNOWN_PROFILE')
            atomic_json(self.config_path, new)
            self.config = new
            self.config_error = None
            self.revision += 1
            self.drop_auto_pending()
            self.event('config_changed', enabled=new['enabled'])
            return self.status()

    def set_enabled(self, value):
        if type(value) is not bool:
            raise QuotaError('INVALID_ENABLED')
        with self.lock:
            config = {**self.config, 'enabled': value}
            if not value:
                # OFF must remain available even after a profile is removed or
                # metadata becomes unreadable. Cancel first, then persist.
                self.config = config
                self.drop_auto_pending()
                self.revision += 1
                try:
                    atomic_json(self.config_path, config)
                except OSError:
                    self.config_error = 'OFF_ACTIVE_CONFIG_SAVE_FAILED'
                self.event('config_changed', enabled=False)
                return self.status()
            return self.configure({'revision': self.revision, 'config': config})

    def status(self):
        with self.lock:
            rows = copy.deepcopy(self.rows)
            now = time.time()
            for row in rows.values():
                age = now - timestamp(row.get('updated_at'))
                if row.get('status') == 'OK' and (age < 0 or age > max(120, self.config['poll_seconds'] * 2)):
                    row['status'] = 'STALE'
            return {'active': self.active, 'active_checked_at': self.active_checked_at,
                    'profiles': rows, 'config': copy.deepcopy(self.config), 'revision': self.revision,
                    'pending': copy.deepcopy(self.pending), 'refreshing': self.refreshing,
                    'switching': self.switching, 'events': copy.deepcopy(self.events[-10:]),
                    'config_error': self.config_error or self.persistence_error, 'epoch': self.epoch,
                    'next_scheduled_at': self.next_scheduled(now),
                    'cancelled_until': self.cancelled_until,
                    'paused_until': self.paused_until,
                    'recovery_required': self.recovery_required,
                    'last_refresh_finished': self.last_refresh_finished,
                    'recommended': self.recommend(now),
                    'transaction': copy.deepcopy(self.transaction),
                    'codex': {**copy.deepcopy(self.codex_state), 'recommended': self.recommend_other(self.codex_state, now)},
                    'agy_stopped': copy.deepcopy(self.agy_stopped), 'agy_target': copy.deepcopy(self.agy_target),
                    'codex_watches': copy.deepcopy(self.codex_watches[-10:]),
                    'codex_events': copy.deepcopy([e for e in self.events if e['kind'].startswith('codex_')][-10:]),
                    'claude': {**copy.deepcopy(self.claude_state), 'recommended': self.recommend_other(self.claude_state, now)},
                    'claude_events': copy.deepcopy([e for e in self.events if e['kind'].startswith('claude_')][-10:]),
                    'gemini_events': copy.deepcopy([e for e in self.events if not e['kind'].startswith('codex_')][-10:]),
                    'activity': copy.deepcopy(self.activity),
                    'enrolling': copy.deepcopy(self.enrolling),
                    'warmup': dict(self.warmup)}

    def ranked(self, now):
        """Rotation candidates, best first: (label, reason). Backups only when no normal account is left."""
        normal, backup = self.tiers(now)
        return normal or backup

    def tiers(self, now):
        config, ranked, backup, first = self.config, [], [], []
        for label in config['participants']:
            row = self.rows.get(label)
            values = metrics(row, config, now)
            wins = gemini_windows(row, config['family'])
            if label == self.active or not values or not wins:
                continue
            (five, _), (weekly, weekly_reset) = wins['5h'], wins['weekly']
            present = [v for v in (five, weekly) if isinstance(v, (int, float))]
            usable = min(present) if present else 0
            item = (weekly if isinstance(weekly, (int, float)) else usable, weekly_reset, wins['5h'][1],
                    (label, why(label, usable, weekly or 0, weekly_reset, now)))
            if urgent(weekly, weekly_reset, now):
                if not isinstance(five, (int, float)) or five >= config['remaining_below']:
                    first.append((weekly_reset, item[3]))  # absolute priority, earliest reset first
                continue
            if min(values) < config['remaining_below']:
                continue
            if isinstance(weekly, (int, float)) and weekly < PRIORITY_HEADROOM:
                if usable >= max(config['remaining_below'], BACKUP_FLOOR):
                    backup.append(item)
            elif usable >= max(config['remaining_below'], PRIORITY_HEADROOM):
                ranked.append(item)
        return [p for _, p in sorted(first, key=lambda x: x[0])] + priority_order(ranked, now), priority_order(backup, now)

    def weekly_reset(self, label):
        wins = gemini_windows(self.rows.get(label), self.config['family'])
        return (wins and wins['weekly'][1]) or float('inf')

    def urgent_label(self, label, now):
        wins = gemini_windows(self.rows.get(label), self.config['family'])
        return bool(wins) and urgent(wins['weekly'][0], wins['weekly'][1], now)

    def recommend_other(self, state, now):
        """The Codex / Claude account the rotator would pick next (same order as Antigravity), or None."""
        ranked = codex_order(state['profiles'], state['active'], now, self.config['remaining_below'])
        return ranked[0] if ranked else None

    def recommend(self, now):
        ranked = self.ranked(now)
        return ranked[0][0] if ranked else None

    def next_scheduled(self, now):
        if (not self.config['enabled'] or not self.config['time_enabled'] or self.recovery_required):
            return None
        # Search actual local clock minutes, including DST changes.
        base = int(max(now, self.paused_until) // 60) * 60
        for offset in range(1, 121):
            candidate = base + offset * 60
            if datetime.fromtimestamp(candidate).minute in self.config['hourly_minutes']:
                return datetime.fromtimestamp(candidate).astimezone().isoformat()
        return None

    def refresh(self, scope=None):
        """scope None = everything (the poll loop); 'gemini' / 'codex' / 'claude' = only that tab's accounts."""
        with self.lock:
            if self.refreshing:
                return
            self.refreshing = True
        try:
            if scope in (None, 'gemini'):
                self._refresh_gemini()
                self.agy_refresh_stopped()
        finally:
            if scope in (None, 'codex'):
                self.refresh_codex()
            if scope in (None, 'claude'):
                self.refresh_claude(force=scope == 'claude')
            if scope is None:
                try:
                    self.check_session_errors(time.time())
                except Exception:
                    pass  # a report only; never breaks polling
            with self.lock:
                self.refreshing = False
                self.last_refresh_finished = stamp()
        try:
            self.warm_idle()
        except Exception:
            pass  # warm-up is best effort; never breaks polling

    def agy_refresh_stopped(self):
        try:
            found = self.agy_continue.stopped()
            with self.lock:
                self.agy_stopped = sorted(
                    ({**f, 'project': os.path.basename((f.get('cwd') or '').rstrip('/')) or f['target']} for f in found),
                    key=lambda f: -(f.get('at') or 0))
                target = self.agy_target
            if target and not self.agy_continue.alive(target):
                with self.lock:
                    if self.agy_target is target:
                        self.agy_target = None
                        self.save_targets()
        except Exception:
            pass  # only feeds the page's button

    def save_targets(self):
        try:
            atomic_json(self.root / 'continue_targets.json', {'claude': self.claude_target, 'agy': self.agy_target})
        except OSError:
            pass

    def continue_sessions(self, provider):
        """What the dialog's picker offers: {'items': [{id, project, state, at}], 'default': id}. state: stopped (on
        the usage limit), running (our continue is working on it), busy (working elsewhere: not offered for a Codex
        thread, resuming it would make two writers), idle."""
        items, default = [], None
        if provider == 'codex':
            with self.lock:
                target = self.codex_current_target()
                running = {w['thread'] for w in self.codex_watches if w['status'] == 'running'}
            for t in sorted(self.watch_io.recent_threads(), key=lambda t: -(t.get('at') or 0)):
                state = ('running' if t['thread'] in running else 'stopped' if t['last'] == 'task_failed'
                         else 'error' if t['last'] == 'task_error'
                         else 'busy' if t['last'] == 'task_started' else 'idle')
                project = os.path.basename((t.get('cwd') or '').rstrip('/')) or t['thread'][-6:]
                items.append({'id': t['thread'], 'state': state, 'at': t.get('at'), 'project': project,
                              'name': t.get('name')})
            default = target and target['thread']
        elif provider == 'claude':
            target = self.claude_current_target()
            for record, hit, at in sorted(self.relinker.sessions(), key=lambda x: -(x[2] or 0)):
                items.append({'id': record['sessionId'], 'at': at,
                              'state': 'stopped' if hit else 'busy' if record.get('status') == 'busy'
                              else 'error' if self.relinker.last_error(record['sessionId']) else 'idle',
                              'project': record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))})
            default = target and target[0]['sessionId']
        elif provider == 'agy':
            with self.lock:
                target = self.agy_target
            for sess in sorted(self.agy_continue.sessions(), key=lambda x: -(x.get('at') or 0)):
                items.append({'id': f"{sess['kind']}:{sess['target']}", 'at': sess.get('at'),
                              'state': 'stopped' if sess['stopped'] else 'idle',
                              'project': os.path.basename((sess.get('cwd') or '').rstrip('/')) or sess['target']})
            default = target and f"{target['kind']}:{target['target']}"
        else:
            raise QuotaError('INVALID_PARAMS')
        ids = [i['id'] for i in items if not (provider == 'codex' and i['state'] == 'busy')]
        if default not in ids:
            default = next((i['id'] for i in items if i['state'] == 'stopped'), ids[0] if ids else None)
        return {'items': items, 'default': default}

    def agy_session(self, session_id):
        kind, _, target = (session_id or '').partition(':')
        sess = next((x for x in self.agy_continue.sessions() if x['kind'] == kind and x['target'] == target), None)
        if not sess:
            raise QuotaError('SESSION_GONE')
        return {**{k: v for k, v in sess.items() if k != 'stopped'},
                'project': os.path.basename((sess.get('cwd') or '').rstrip('/')) or sess['target']}

    def agy_continue_now(self, text=None, session=None):
        """The user asked (from the page) to type "continue" into the agy session that stopped on the limit last."""
        text = continue_text(text)
        with self.lock:
            if self.switching or self.agy_continuing:
                raise QuotaError('SWITCH_BUSY')
            if not self.active or gemini_spent(self.rows.get(self.active) or {}, 'gemini'):
                raise QuotaError('ACTIVE_EXHAUSTED')
            target = self.agy_session(session) if session else self.agy_target
            if not target and not self.agy_stopped:
                raise QuotaError('THREAD_NOT_STOPPED')
            if target:  # the chosen session, or the one already being continued: keep talking to it
                items, self.agy_continuing = [target], True
            else:
                items, self.agy_stopped, self.agy_continuing = self.agy_stopped[:1], self.agy_stopped[1:], True
            active = self.active
        threading.Thread(target=self._agy_continue_now, args=(items, active, text, bool(target)),
                         daemon=True).start()
        return self.status()

    def _agy_continue_now(self, items, active, text, to_target=False):
        try:
            for item in items:
                error = self.agy_continue.send(item, text, check=not to_target)
                with self.lock:
                    if error:
                        self.event('agy_manualcontinue_failed', project=item['project'], error=error)
                    else:
                        self.agy_target = item
                        self.save_targets()
                        self.event('agy_manualcontinue', project=item['project'], selected=active)
        finally:
            with self.lock:
                self.agy_continuing = False

    def _refresh_gemini(self):
        try:
            labels = self.profiles.list()
            active = self.profiles.active()
            with self.lock:
                if not self.switching:
                    if self.active and active != self.active:
                        self.event('drift', previous=self.active, selected=active)
                    self.active = active
                    self.active_checked_at = stamp()
                self.rows = {k: self.rows.get(k, {'label': k, 'status': 'UNAVAILABLE', 'groups': []})
                             for k in labels}
            for label in labels:
                if self.stop.is_set():
                    break
                try:
                    row = self.client.fetch(label)
                except Exception as e:
                    error = str(e) if isinstance(e, QuotaError) else 'QUOTA_INTERNAL_ERROR'
                    with self.lock:
                        row = copy.deepcopy(self.rows.get(label, {'label': label, 'groups': []}))
                    age = time.time() - (timestamp(row.get('updated_at')) or 0)
                    if (row.get('status') == 'OK' and transient(error)
                            and age <= max(120, self.config['poll_seconds'] * 2)):
                        # One blip: keep the still-fresh numbers (policy uses them only while fresh).
                        row['last_error'] = error
                    else:
                        # Old numbers may stay visible with STALE, but cannot drive policy.
                        row.update(status='STALE' if row.get('updated_at') else 'UNAVAILABLE', error=error)
                with self.lock:
                    self.rows[label] = row
        except QuotaError as e:
            with self.lock:
                self.active = None
                self.rows = {}
                self.event('quota_error', error=str(e))

    def refresh_codex(self):
        try:
            data = self.codex.quota()
        except QuotaError as e:
            with self.lock:
                self.codex_state['error'] = str(e)
            return
        except Exception:
            with self.lock:
                self.codex_state['error'] = 'CODEX_INTERNAL_ERROR'
            return
        with self.lock:
            if not self.codex_state['switching']:
                self.codex_state.update(profiles=data['profiles'], active=data['active'],
                                        updated_at=stamp(), error=None)
        self.codex_auto(time.time())
        self.codex_refresh_stopped()

    def codex_refresh_stopped(self):
        """Threads stopped on the usage limit, offered on the page for a manual continue."""
        try:
            found = list(self.watch_io.stopped_threads())
            with self.lock:
                running = {w['thread'] for w in self.codex_watches if w['status'] == 'running'}
                self.codex_state['stopped'] = sorted(
                    ({**t, 'project': os.path.basename((t.get('cwd') or '').rstrip('/')) or t['thread'][-6:]}
                     for t in found if t['thread'] not in running), key=lambda t: -(t.get('at') or 0))
                self.codex_state['target'] = self.codex_current_target()
        except Exception:
            pass  # best effort; never breaks polling

    def codex_current_target(self):
        """The thread last continued (by ▶ or automatically), until it hits the usage limit again: ▶ keeps talking
        to it instead of picking another stopped thread (2026-10-12)."""
        last = self.codex_watches[-1] if self.codex_watches else None
        if not last or last.get('status') == 'limited' or not last.get('started_at') \
                or time.time() - timestamp(last['started_at']) > 24 * 3600:
            return None
        return {'thread': last['thread'], 'project': last['project'], 'running': last['status'] == 'running',
                'tmux': last.get('tmux')}

    def codex_thread_info(self, thread):
        """(cwd, model) of a thread, from its rollout."""
        try:
            path = self.watch_io.rollout(thread)
            with open(path) as f:
                meta = json.loads(f.readline()).get('payload') or {}
            return meta.get('cwd'), self.watch_io.thread_model(path)
        except Exception:
            return None, None

    def codex_continue_now(self, thread, text=None):
        """The user asked (from the page) to continue the thread that stopped on the limit last, on the live account."""
        text = continue_text(text)
        with self.lock:
            target = self.codex_current_target()
        if target and thread in (None, target['thread']):
            return self.codex_send_to_target(target, text)
        if thread is not None and not any(t['thread'] == thread for t in self.codex_state['stopped']):
            return self.codex_send_to_thread(thread, text)  # picked in the dialog: any recent thread
        with self.lock:
            state = self.codex_state
            items = [t for t in state['stopped'] if thread in (None, t['thread'])][:1]  # the latest one only
            if not items:
                raise QuotaError('THREAD_NOT_STOPPED')
            if state['switching'] or self.codex_manual_continuing:
                raise QuotaError('SWITCH_BUSY')
            active = state['active']
            if not active or codex_exhausted(state['profiles'].get(active) or {}):
                raise QuotaError('ACTIVE_EXHAUSTED')
            ids = {t['thread'] for t in items}
            state['stopped'] = [t for t in state['stopped'] if t['thread'] not in ids]  # the rest stay offered
            if any(w['thread'] in ids for w in self.codex_waiting):
                self.codex_waiting = [w for w in self.codex_waiting if w['thread'] not in ids]
                self.save_waiting()
            self.codex_manual_continuing = True
        threading.Thread(target=self._codex_continue_now, args=(items, active, text), daemon=True).start()
        return self.status()

    def codex_send_to_thread(self, thread, text):
        """Resume a thread picked in the dialog with the text, unless it is working elsewhere right now."""
        found = next((t for t in self.watch_io.recent_threads() if t['thread'] == thread), None)
        if not found:
            raise QuotaError('SESSION_GONE')
        if found['last'] == 'task_started':
            raise QuotaError('THREAD_BUSY')  # ChatGPT or another CLI is working on it: two writers otherwise
        cwd, model = self.codex_thread_info(thread)
        with self.lock:
            state = self.codex_state
            if state['switching'] or self.codex_manual_continuing:
                raise QuotaError('SWITCH_BUSY')
            active = state['active']
            if not active or codex_exhausted(state['profiles'].get(active) or {}):
                raise QuotaError('ACTIVE_EXHAUSTED')
            self.codex_manual_continuing = True
        threading.Thread(target=self._codex_continue_now, args=([{'thread': thread, 'cwd': cwd, 'model': model}],
                                                                active, text), daemon=True).start()
        return self.status()

    def continue_preview(self, provider, session=None):
        """The conversation ▶ would send to and its latest output (read only), for the dialog."""
        limit = 20000  # characters shown: the end of a very long answer
        if session:
            if provider == 'codex':
                cwd, _ = self.codex_thread_info(session)
                text, at = self.watch_io.last_output(session)
                project = os.path.basename((cwd or '').rstrip('/')) or session[-6:]
            elif provider == 'claude':
                found = self.relinker.session(session)
                if not found:
                    raise QuotaError('SESSION_GONE')
                record = found[0]
                text, at = self.relinker.last_output(record)
                project = record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))
            elif provider == 'agy':
                pick = self.agy_session(session)
                text, at, project = self.agy_continue.last_output(pick), None, pick['project']
            else:
                raise QuotaError('INVALID_PARAMS')
            cut = bool(text) and len(text) > limit
            return {'project': project, 'text': text[-limit:] if text else None, 'at': at, 'cut': cut, 'target': True}
        if provider == 'codex':
            with self.lock:
                target = self.codex_current_target()
                stopped = list(self.codex_state['stopped'])
            pick = target or (stopped[0] if stopped else None)
            if not pick:
                return None
            text, at = self.watch_io.last_output(pick['thread'])
            project = pick['project']
        elif provider == 'claude':
            target = self.claude_current_target()
            if target:
                record, project = target
            else:
                stopped = self.claude_latest_first(self.relinker.stopped_sessions())
                if not stopped:
                    return None
                record = stopped[0][1][0]
                project = record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))
            text, at = self.relinker.last_output(record)
        elif provider == 'agy':
            with self.lock:
                target = self.agy_target
                pick = target or (self.agy_stopped[0] if self.agy_stopped else None)
            if not pick:
                return None
            text, at, project = self.agy_continue.last_output(pick), None, pick['project']
        else:
            raise QuotaError('INVALID_PARAMS')
        cut = bool(text) and len(text) > limit
        return {'project': project, 'text': text[-limit:] if text else None, 'at': at, 'cut': cut,
                'target': bool(target)}

    def codex_send_to_target(self, target, text):
        with self.lock:
            state = self.codex_state
            if state['switching'] or self.codex_manual_continuing:
                raise QuotaError('SWITCH_BUSY')
            active = state['active']
            if not active or codex_exhausted(state['profiles'].get(active) or {}):
                raise QuotaError('ACTIVE_EXHAUSTED')
            if not target['running']:
                self.codex_manual_continuing = True
        if target['running']:  # its CLI is still working: the prompt is queued there
            try:
                self.watch_io.type(target['tmux'], text)
            except Exception:
                raise QuotaError('TYPE_FAILED') from None
            with self.lock:
                self.event('codex_manualcontinue', project=target['project'], selected=active, queued=True)
            return self.status()
        cwd, model = self.codex_thread_info(target['thread'])
        item = {'thread': target['thread'], 'cwd': cwd, 'model': model}
        threading.Thread(target=self._codex_continue_now, args=([item], active, text), daemon=True).start()
        return self.status()

    def _codex_continue_now(self, items, active, text):
        try:
            self.codex_continue([{k: t.get(k) for k in ('thread', 'cwd', 'model')} for t in items], active,
                                kind='codex_manualcontinue', text=text)
        finally:
            with self.lock:
                self.codex_manual_continuing = False

    def refresh_claude(self, now=None, force=False):
        now = time.time() if now is None else now
        if now < self.claude_next_poll and not force:
            return
        base = self.config['poll_seconds']
        interval = max(self.claude_interval, base)
        try:
            data = self.claude.quota()
        except QuotaError as e:
            with self.lock:
                self.claude_state['error'] = str(e)
                self.claude_next_poll = now + 120
            return
        except Exception:
            with self.lock:
                self.claude_state['error'] = 'CLAUDE_INTERNAL_ERROR'
                self.claude_next_poll = now + 120
            return
        with self.lock:
            if self.claude_state['switching']:
                return
            old, profiles, limited = self.claude_state['profiles'], {}, False
            for label, row in data['profiles'].items():
                status = row.get('status')
                limited = limited or status == 'HTTP_429'
                if status != 'OK' and claude_transient(status) and (old.get(label) or {}).get('status') == 'OK':
                    # rate limit / network blip: keep the last numbers (marked stale), never warm or decide on them
                    profiles[label] = {**old[label], 'active': row.get('active', old[label].get('active')), 'stale': status}
                else:
                    profiles[label] = row
            self.claude_state.update(profiles=profiles, active=data['active'], error=None,
                                     updated_at=self.claude_state['updated_at'] if limited else stamp())
            if limited:
                self.claude_interval, self.claude_good = min(max(interval * 2, 120), CLAUDE_MAX_INTERVAL), 0
            else:
                self.claude_good += 1
                if self.claude_good >= 10 and interval > base:
                    self.claude_interval, self.claude_good = max(base, interval // 2), 0
            self.claude_next_poll = now + max(self.claude_interval, base)
        try:
            stopped = self.claude_latest_first(self.relinker.stopped_sessions())
            target = self.claude_current_target()
            with self.lock:
                self.claude_state['stopped'] = [r.get('name') or os.path.basename((r.get('cwd') or '').rstrip('/'))
                                                for _, (r, _) in stopped]
                self.claude_state['target'] = target and {'sessionId': target[0]['sessionId'], 'project': target[1]}
        except Exception:
            pass  # only feeds the page's "送 continue" button
        try:
            self.claude_auto(now)
        except Exception:
            with self.lock:
                self.claude_continuing = False  # best effort; never breaks polling

    def claude_use(self, params, source='manual'):
        label = params.get('label')
        with self.lock:
            if not isinstance(label, str) or label not in self.claude_state['profiles']:
                raise QuotaError('UNKNOWN_PROFILE')
        if not self.claude_lock.acquire(blocking=False):
            raise QuotaError('SWITCH_BUSY')
        try:
            with self.lock:
                self.claude_state['switching'] = True
            try:  # note the remote-controlled sessions before the switch, to bring each one back afterwards
                remote_before = self.relinker.remote_sessions()
            except Exception:
                remote_before = None
            result = self.claude.use(label)
            with self.lock:
                self.claude_state['active'] = result['selected']
                for name, r in self.claude_state['profiles'].items():
                    r['active'] = name == result['selected']
                self.event('claude_switched', selected=result['selected'], source=source)
            if source == 'manual':
                self.claude_hand_over()
            if result.get('activation') != 'UNCHANGED':  # 重設 onto the live account: links are still right
                with self.lock:
                    self.claude_relinking = True  # set here so no poll can type "continue" before the relink starts
                    self.claude_relink_gen += 1
                    gen = self.claude_relink_gen
                threading.Thread(target=self.claude_relink, args=(remote_before, gen),
                                 daemon=True).start()
            return result
        except QuotaError as e:
            with self.lock:
                self.event('claude_switch_failed', error=str(e))
            raise
        finally:
            with self.lock:
                self.claude_state['switching'] = False
            self.claude_lock.release()
            self.refresh_requested.set()

    def claude_relink(self, before=None, gen=None):
        """Remote Control links belong to the old account after a switch: register them again (see claude_remote)."""
        stop = lambda: gen is not None and gen != self.claude_relink_gen
        def report(record, link, error):
            with self.lock:
                name = record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))
                if error:
                    self.event('claude_remote_failed', project=name, error=error)
                    if self.config['notify']:
                        self.notifier(f"轉 Claude 帳號後，{name} 重連咗遙控但仍然屬於舊帳號：要重開佢（claude --resume）先會轉"
                                      if error == 'OLD_LOGIN' else
                                      f"轉 Claude 帳號後，未能自動重連 {name} 嘅遙控（{error}）：請喺嗰個 session 打 /remote-control")
                else:
                    self.event('claude_remote_relinked', project=name, summary=f'https://claude.ai/code/{link}')
        started = time.time()
        try:
            self.relinker.run(report, before, stop=stop)
        except Exception:
            with self.lock:
                self.event('claude_remote_failed', project='', error='RELINK_ERROR')
        finally:
            try:  # sessions pick up the new login before anything is typed into them ("continue" included)
                left = PICKUP - (time.time() - started)
                if left > 0:
                    self.relinker.io.sleep(left)
            finally:
                with self.lock:
                    if gen is None or gen == self.claude_relink_gen:  # a newer relink keeps the flag
                        self.claude_relinking = False

    def claude_auto(self, now):
        """Claude Code sessions stopped on the usage limit get "continue" once the live account has quota again (its
        window refilled; a switch by hand hands what stopped over to the user, see claude_hand_over). Claude never switches by itself: until then
        it waits (claude_waiting_quota) and checks again every poll."""
        with self.lock:
            state = self.claude_state
            if not state['waitcontinue']:
                return
        self.claude_check_replies(now)
        with self.lock:
            if state['switching'] or self.claude_relinking or self.claude_continuing or not state['active']:
                return
            active, row = state['active'], copy.deepcopy(state['profiles'].get(state['active']) or {})
        if row.get('stale') or row.get('status') != 'OK':
            return  # rate-limited / failed poll: old numbers decide nothing
        try:
            stopped = self.relinker.stopped_sessions()
        except Exception:
            return
        with self.lock:
            ids = {r['sessionId'] for r, _ in stopped.values()}
            self.claude_continued = {k: v for k, v in self.claude_continued.items()
                                     if k in ids or v.get('pending')}  # got past it (and its answer was seen)
            due = {pid: (r, uuid) for pid, (r, uuid) in stopped.items() if self.claude_due(r['sessionId'], uuid, now)}
            if not due:
                return
            if not codex_available(row):
                if not self.claude_waiting:
                    self.claude_waiting = True
                    self.event('claude_waiting_quota', selected=active, count=len(due))
                return
            self.claude_waiting = False
            self.claude_continuing = True
        threading.Thread(target=self.claude_continue, args=(due, active), daemon=True).start()

    def claude_check_replies(self, now):
        """Typing "continue" is not continuing: only a new answer from the session counts (claude_continue_done);
        the usage limit again, or no answer at all, is reported."""
        with self.lock:
            pending = {k: dict(v) for k, v in self.claude_continued.items() if v.get('pending')}
        for sid, v in pending.items():
            try:
                uuid, hit = self.relinker.last_reply(sid)
            except Exception:
                continue
            if uuid and uuid != v['uuid']:
                error = None if hit else self.relinker.last_error(sid)
                kind = 'claude_continue_limited' if hit else 'claude_continue_error' if error and error['uuid'] == uuid \
                    else 'claude_continue_done'
            elif now - v['at'] > CLAUDE_REPLY_WAIT:
                kind = 'claude_continue_noreply'
            else:
                continue
            with self.lock:
                if sid in self.claude_continued:
                    self.claude_continued[sid]['pending'] = False
                if kind == 'claude_continue_error':
                    self.session_errors[f"claude:{sid}:{uuid}"] = time.time()
                    self.save_session_errors()
                self.event(kind, project=v.get('project', ''),
                           **({'error': error['text'][:200]} if kind == 'claude_continue_error' else {}))
                if kind != 'claude_continue_done' and self.config['notify']:
                    self.notifier(f"Claude 喺 {v.get('project', '')} 打咗 continue，但" +
                                  ('又撞到用量上限。' if kind == 'claude_continue_limited' else
                                   f"出錯停咗：{error['text'][:120]}" if kind == 'claude_continue_error' else
                                   '30 分鐘都冇回應：請自己睇睇。'))

    def check_session_errors(self, now, window=3600):
        """Codex threads and Claude sessions that stopped on an error other than the usage limit (the model at
        capacity, a connection lost, a login that expired, ...) look finished from outside; each such stop is reported
        once (codex_session_error / claude_session_error, with a notification). Only stops of the last hour: an old
        one found after a restart is not news. Then each gets "continue" again (error_retry)."""
        found = []  # (report key, kind, name, label, text, provider, session, stopped at, retry item)
        for t in self.watch_io.recent_threads(max_age=window):
            if t['last'] == 'task_error' and now - (t.get('at') or 0) <= window:
                project = os.path.basename((t.get('cwd') or '').rstrip('/')) or t['thread'][-6:]
                found.append((f"codex:{t['thread']}:{t['at']}", 'codex_session_error', 'Codex', t.get('name') or project,
                              t.get('error') or '', 'codex', t['thread'], t['at'], project))
        for record, _, _ in self.relinker.sessions():
            error = self.relinker.last_error(record['sessionId'])
            if not error:
                continue
            try:
                at = timestamp_of(error['timestamp'])
            except (TypeError, ValueError):
                continue
            if now - at <= window:
                project = record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))
                found.append((f"claude:{record['sessionId']}:{error['uuid']}", 'claude_session_error', 'Claude', project,
                              error['text'] or error.get('error') or '', 'claude', record['sessionId'], at, record))
        with self.lock:
            new = [f for f in found if f[0] not in self.session_errors]
            for key, kind, name, label, text, *_ in new:
                self.session_errors[key] = now
                self.event(kind, project=label, error=text[:200])
                if self.config['notify']:
                    self.notifier(f"{name} 喺 {label} 出錯停咗：{text[:120] or '未知錯誤'}。工作未完成，"
                                  f"{ERROR_RETRY_AFTER // 60} 分鐘後會自動再送 continue。")
            if new or any(now - v > 2 * 86400 for v in self.session_errors.values()):
                self.session_errors = {k: v for k, v in self.session_errors.items() if now - v <= 2 * 86400}
                self.save_session_errors()
            stopped = {f'{f[5]}:{f[6]}' for f in found}
            for k in [k for k in self.error_retries if k not in stopped]:
                del self.error_retries[k]  # it moved on (answered, or someone typed): a later error starts again
        for key, kind, name, label, text, provider, session, at, item in found:
            if now - at >= ERROR_RETRY_AFTER:
                self.error_retry(provider, session, key, label, item, now)

    def error_retry(self, provider, session, stop, label, item, now):
        """Type "continue" into a session that stopped on an error while its account still had quota (so not the usage
        limit: something passing such as the model at capacity), on the live account, once per stop and at most
        ERROR_RETRY_TRIES times in a row. Nothing is sent while the live account is used up (that is the quota
        waiting's job) or while another continue / switch runs; it is tried again on the next poll."""
        slot = f'{provider}:{session}'
        with self.lock:
            seen = self.error_retries.get(slot) or {'stop': None, 'tries': 0}
            if seen['stop'] == stop:
                return
            if seen['tries'] >= ERROR_RETRY_TRIES:
                if seen.get('gave_up') != stop:
                    seen['gave_up'] = stop
                    self.error_retries[slot] = seen
                    self.event(f'{provider}_errorretry_gaveup', project=label, tries=seen['tries'])
                    if self.config['notify']:
                        self.notifier(f"{'Codex' if provider == 'codex' else 'Claude'} 喺 {label} 連續 "
                                      f"{seen['tries']} 次出錯，唔再自動重試，請自己睇睇。")
                return
            if provider == 'codex':
                state = self.codex_state
                active = state['active']
                if state['switching'] or self.codex_manual_continuing or not active \
                        or codex_exhausted(state['profiles'].get(active) or {}):
                    return
                self.codex_manual_continuing = True
            else:
                state = self.claude_state
                active = state['active']
                if state['switching'] or self.claude_relinking or self.claude_continuing or not active \
                        or not codex_available(state['profiles'].get(active) or {}):
                    return
                self.claude_continuing = True
            self.error_retries[slot] = {'stop': stop, 'tries': seen['tries'] + 1}
        if provider == 'codex':
            cwd, model = self.codex_thread_info(session)
            threading.Thread(target=self._codex_error_retry, args=({'thread': session, 'cwd': cwd, 'model': model},
                                                                  active), daemon=True).start()
        else:
            threading.Thread(target=self._claude_error_retry, args=(item, label, active), daemon=True).start()

    def _codex_error_retry(self, item, active):
        try:
            self.codex_continue([item], active, kind='codex_errorretry')
        finally:
            with self.lock:
                self.codex_manual_continuing = False

    def _claude_error_retry(self, record, name, selected):
        try:
            try:
                reply, _ = self.relinker.last_reply(record['sessionId'])
            except Exception:
                reply = None
            error = self.relinker.resume(record)
            with self.lock:
                if error:
                    self.event('claude_autocontinue_failed', project=name, error=error)
                else:
                    self.claude_continued[record['sessionId']] = {'uuid': reply, 'tries': 0, 'at': time.time(),
                                                                  'project': name, 'pending': True}
                    self.event('claude_errorretry', project=name, selected=selected)
        except Exception:
            with self.lock:
                self.event('claude_autocontinue_failed', project=name, error='CONTINUE_ERROR')
        finally:
            with self.lock:
                self.claude_continuing = False

    def save_session_errors(self):
        try:
            atomic_json(self.root / 'session_errors.json', self.session_errors)
        except OSError:
            pass

    def claude_due(self, session_id, uuid, now):
        seen = self.claude_continued.get(session_id)
        if not seen:
            return True
        if seen['uuid'] == uuid:
            return False  # already continued this stop (it may still be on its way)
        return seen['tries'] < CLAUDE_CONTINUE_TRIES and now - seen['at'] >= CLAUDE_CONTINUE_GAP

    def claude_hand_over(self):
        """A switch by hand: the sessions stopped on the limit now are the user's, never continued automatically
        (only a later stop, or the page's ▶, types into them)."""
        try:
            stopped = self.relinker.stopped_sessions()
        except Exception:
            return
        with self.lock:
            for record, uuid in stopped.values():
                self.claude_continued[record['sessionId']] = {'uuid': uuid, 'tries': 0, 'at': time.time(),
                                                              'project': record.get('name') or '', 'pending': False}
            self.claude_waiting = False
            if stopped:
                self.event('claude_waiting_cleared', count=len(stopped))

    def claude_current_target(self):
        """(record, name) of the session last continued, until it hits the usage limit again: ▶ keeps talking to it."""
        with self.lock:
            target = self.claude_target
        if not target:
            return None
        try:
            found = self.relinker.session(target['sessionId'])
            found = found if isinstance(found, tuple) and len(found) == 2 else None
        except Exception:
            found = None
        if not found or (found[1] and time.time() - target['at'] > 60):  # ended, or stopped on the limit again
            with self.lock:
                if self.claude_target is target:
                    self.claude_target = None
                    self.save_targets()
            return None
        record = found[0]
        return record, record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))

    def claude_latest_first(self, stopped):
        """[(pid, (record, uuid))], the session that stopped last first."""
        def at(record):
            try:
                value = self.relinker.stopped_at(record)
                return value if isinstance(value, (int, float)) else 0
            except Exception:
                return 0
        return sorted(stopped.items(), key=lambda kv: -at(kv[1][0]))

    def claude_continue_now(self, text=None, session=None):
        """The user asked (from the page) to type "continue" into the session that stopped on the limit last."""
        text = continue_text(text)
        with self.lock:
            state = self.claude_state
            if state['switching'] or self.claude_relinking or self.claude_continuing:
                raise QuotaError('SWITCH_BUSY')
            active = state['active']
            if not active or not codex_available(state['profiles'].get(active) or {}):
                raise QuotaError('ACTIVE_EXHAUSTED')
        if session:
            found = self.relinker.session(session)
            if not found:
                raise QuotaError('SESSION_GONE')
            record = found[0]
            target = record, record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))
        else:
            target = self.claude_current_target()
        if target:
            with self.lock:
                self.claude_continuing = True
            threading.Thread(target=self.claude_send_to_target, args=(target, active, text), daemon=True).start()
            return self.status()
        stopped = dict(self.claude_latest_first(self.relinker.stopped_sessions())[:1])  # the latest one only
        if not stopped:
            raise QuotaError('THREAD_NOT_STOPPED')
        with self.lock:
            if self.claude_continuing:
                raise QuotaError('SWITCH_BUSY')
            self.claude_continuing = True
            self.claude_state['stopped'] = []
        threading.Thread(target=self.claude_continue, args=(stopped, active, 'claude_manualcontinue', text),
                         daemon=True).start()
        return self.status()

    def claude_send_to_target(self, target, selected, text):
        record, name = target
        try:
            try:
                reply, _ = self.relinker.last_reply(record['sessionId'])
            except Exception:
                reply = None
            error = self.relinker.resume(record, text)
            with self.lock:
                if error:
                    self.event('claude_autocontinue_failed', project=name, error=error)
                else:
                    self.claude_continued[record['sessionId']] = {'uuid': reply, 'tries': 0, 'at': time.time(),
                                                                  'project': name, 'pending': True}
                    self.claude_target = {'sessionId': record['sessionId'], 'at': time.time()}
                    self.save_targets()
                    self.event('claude_manualcontinue', project=name, selected=selected)
        except Exception:
            with self.lock:
                self.event('claude_autocontinue_failed', project=name, error='CONTINUE_ERROR')
        finally:
            with self.lock:
                self.claude_continuing = False

    def claude_continue(self, due, selected, kind='claude_autocontinue', text='continue'):
        try:
            still = self.relinker.stopped_sessions()
            for pid, (record, uuid) in due.items():
                if pid not in still or still[pid][1] != uuid:
                    continue  # it moved on meanwhile (continued by hand, or ended)
                name = record.get('name') or os.path.basename((record.get('cwd') or '').rstrip('/'))
                error = self.relinker.resume(record) if text == 'continue' else self.relinker.resume(record, text)
                with self.lock:
                    seen = self.claude_continued.get(record['sessionId'], {'tries': 0})
                    self.claude_continued[record['sessionId']] = {'uuid': uuid, 'tries': seen['tries'] + 1,
                                                                  'at': time.time(), 'project': name,
                                                                  'pending': not error}
                    if error:
                        self.event('claude_autocontinue_failed', project=name, error=error)
                        if self.config['notify']:
                            self.notifier(f"Claude 有額度返，但未能喺 {name} 自動打 continue（{error}）：請自己接續")
                    else:
                        self.claude_target = {'sessionId': record['sessionId'], 'at': time.time()}
                        self.save_targets()
                        self.event(kind, project=name, selected=selected)
        except Exception:
            with self.lock:
                self.event('claude_autocontinue_failed', project='', error='CONTINUE_ERROR')
        finally:
            with self.lock:
                self.claude_continuing = False

    def claude_waitcontinue_set(self, value):
        if type(value) is not bool:
            raise QuotaError('INVALID_ENABLED')
        with self.lock:
            self.claude_state['waitcontinue'] = value
            atomic_json(self.root / 'claude.json', {'waitcontinue': value})
            self.claude_waiting = False
            self.event('claude_waitcontinue_changed', enabled=value)
            return self.status()

    def codex_auto(self, now):
        """Switch only when the live Codex account is used up and another one still has capacity (auto); continue what
        stopped once the live account has quota again (waitcontinue) - each setting works without the other."""
        with self.lock:
            state = self.codex_state
            if (not (state['auto'] or state['waitcontinue']) or state['switching'] or self.codex_manual_continuing
                    or now - self.last_codex_switch < CODEX_COOLDOWN):
                return
            active, rows = state['active'], state['profiles']
            refilled, waiting, candidates, capture = not active or not codex_exhausted(rows.get(active, {})), [], [], False
            if refilled:
                self.codex_all_exhausted = False
                if state['waitcontinue'] and active and rows.get(active, {}).get('status') == 'OK' and self.codex_waiting:
                    waiting, self.codex_waiting = self.codex_waiting, []
                    self.save_waiting()
            else:
                refill = reset_epoch((rows[active].get('5h') or {}).get('reset_at'))
                if state['auto'] and refill and 0 < refill - now <= STAY_WINDOW:
                    return  # live account refills within 15 min: do not close ChatGPT / Codex for that
                candidates = codex_order(rows, active, now, self.config['remaining_below']) if state['auto'] else []
                if not candidates and not self.codex_all_exhausted:
                    self.codex_all_exhausted = True
                    self.event('codex_all_exhausted' if state['auto'] else 'codex_waiting_quota')
                    capture = state['waitcontinue']
        if refilled:
            if waiting:  # the live account refilled: continue what stopped while everything was used up
                self.codex_continue(waiting, active)
            return
        if not candidates:
            if capture:  # note what just stopped (it only looks interrupted for a few minutes)
                try:
                    found = self.watch_io.interrupted_threads()
                except Exception:
                    found = []
                with self.lock:
                    known = {w['thread'] for w in self.codex_waiting}
                    for item in found:
                        if item['thread'] not in known:
                            try:
                                path = self.watch_io.rollout(item['thread'])
                                size = self.watch_io.size(path) if path else None
                            except OSError:
                                size = None
                            self.codex_waiting.append({**item, 'size': size})
                    self.save_waiting()
            return
        with self.lock:
            target = candidates[0]
            autocontinue, waitcontinue = state['autocontinue'], state['waitcontinue']
        # Capture interrupted threads before the switch closes ChatGPT / Codex CLI.
        try:
            app_was_running = self.codex.app_running()
        except Exception:
            app_was_running = False
        captured = []
        if autocontinue:
            try:
                captured = self.watch_io.interrupted_threads()
            except Exception:
                captured = []
        if waitcontinue:  # an account got quota back after all were used up: what stopped then goes on too
            with self.lock:
                waiting, self.codex_waiting = self.still_stopped(self.codex_waiting), []
                self.save_waiting()
            captured += [w for w in waiting if w['thread'] not in {c['thread'] for c in captured}]
        try:
            self.codex_use({'label': target}, source='automatic')
        except QuotaError:
            return  # Recorded as codex_switch_failed; the cooldown stops retrying every poll.
        for item in captured:
            project = os.path.basename((item.get('cwd') or '').rstrip('/')) or item['thread'][-6:]
            try:
                self.codex.continue_thread(item['thread'], item.get('model'))
                with self.lock:
                    self.autocontinue_event(project, target)
            except QuotaError as e:
                with self.lock:
                    self.event('codex_autocontinue_failed', project=project, error=str(e))
        self.codex_app_safety_net(app_was_running)

    def still_stopped(self, items):
        """Waiting threads nobody continued meanwhile: no new turn since they were noted, or only one that ended on the
        usage limit again. Not the file size: ChatGPT writes thread_settings_applied when it opens the thread
        (2026-10-12 11:12 that dropped the waiting thread without a continue)."""
        out = []
        for w in items:
            try:
                path = self.watch_io.rollout(w['thread'])
                if path and (w.get('size') is None or self.watch_io.turn(path, w['size'])[0] in (None, 'task_failed')):
                    out.append(w)
                elif path:
                    with self.lock:
                        self.event('codex_waiting_dropped', project=os.path.basename((w.get('cwd') or '').rstrip('/')))
            except OSError:
                continue
        return out

    def save_waiting(self):
        try:
            atomic_json(self.root / 'codex_waiting.json', self.codex_waiting)
        except OSError:
            pass

    def codex_continue(self, waiting, selected, kind='codex_autocontinue', text='continue'):
        """The live Codex account has quota again: continue the threads that stopped while all were used up."""
        items = self.still_stopped(waiting)
        if not items:
            return
        try:
            app_was_running = self.codex.app_running()
        except Exception:
            app_was_running = False
        for item in items:
            project = os.path.basename((item.get('cwd') or '').rstrip('/')) or item['thread'][-6:]
            try:
                extra = {'prompt': text} if text != 'continue' else {}
                self.codex.continue_thread(item['thread'], item.get('model'), **extra)
                with self.lock:
                    self.autocontinue_event(project, selected, kind)
            except QuotaError as e:
                with self.lock:
                    self.event('codex_autocontinue_failed', project=project, error=str(e))
        self.codex_app_safety_net(app_was_running)

    def codex_app_safety_net(self, app_was_running, settle=10):
        """ChatGPT was open before an automatic switch: make sure it is open after it and its continues (it hosts
        remote control). 2026-10-05 19:44 a failed continue left it closed for 2.5 h."""
        if not app_was_running:
            return
        for _ in range(settle):
            try:
                if self.codex.app_running():
                    return
            except Exception:
                return
            time.sleep(1)
        opened = False
        try:
            opened = self.codex.open_app()
        except Exception:
            pass
        with self.lock:
            self.event('codex_app_reopened' if opened else 'codex_app_reopen_failed')

    ADD_LABEL = {'gemini': re.compile(r'^GEMINI_[A-Z0-9_]{1,20}$'), 'codex': re.compile(r'^CODEX_[A-Z0-9_]{1,20}$'),
                 'claude': re.compile(r'^CLAUDE_[A-Z0-9_]{1,20}$')}

    def account_add_start(self, params):
        provider, label = params.get('provider'), params.get('label')
        if provider not in self.ADD_LABEL or not isinstance(label, str) or not self.ADD_LABEL[provider].match(label):
            raise QuotaError('INVALID_LABEL')
        with self.lock:
            if self.enrolling:
                raise QuotaError('ENROLLMENT_IN_PROGRESS')
            if provider == 'gemini' and self.switching:
                raise QuotaError('SWITCH_BUSY')
        if provider == 'gemini' and self.busy():
            raise QuotaError('AGY_BUSY_TRY_LATER')  # signing in closes agy / Antigravity
        with self.lock:
            self.enrolling = {'provider': provider, 'label': label, 'stage': 'starting', 'started_at': stamp()}
        try:
            result = self.adder.start(provider, label)
        except QuotaError as e:
            with self.lock:
                self.enrolling = None
                self.event('account_add_failed', provider=provider, project=label, error=str(e))
            raise
        with self.lock:
            self.enrolling.update(stage='waiting_device' if provider == 'codex' else 'waiting_code',
                                  url=result.get('url'), code=result.get('code'))
            return self.status()

    def account_added(self, provider, label):
        # caller holds self.lock
        if provider == 'gemini' and label not in self.config['participants']:
            self.config['participants'] = self.config['participants'] + [label]
            try:
                atomic_json(self.config_path, self.config)
            except OSError:
                pass
            self.revision += 1
        self.enrolling = None
        self.event('account_added', provider=provider, selected=label)
        self.refresh_requested.set()

    def account_add_finish(self, params):
        with self.lock:
            state = self.enrolling
        if not state or state['provider'] not in ('gemini', 'claude'):
            raise QuotaError('NO_ENROLLMENT')
        try:
            result = self.adder.finish(state['provider'], params.get('code'))
        except QuotaError as e:
            with self.lock:
                self.enrolling = None
                self.event('account_add_failed', provider=state['provider'], project=state['label'], error=str(e))
                self.refresh_requested.set()
            raise
        with self.lock:
            self.account_added(state['provider'], result.get('enrolled') or state['label'])
            return self.status()

    def account_add_cancel(self):
        with self.lock:
            state = self.enrolling
        if state:
            try:
                self.adder.cancel(state['provider'])
            except QuotaError:
                pass
        with self.lock:
            self.enrolling = None
            if state:
                self.event('account_add_cancelled', provider=state['provider'], project=state['label'])
            return self.status()

    def account_remove(self, params):
        """Forget an enrolled account (never the live one); Gemini also leaves the rotation list."""
        provider, label = params.get('provider'), params.get('label')
        if provider not in self.ADD_LABEL or not isinstance(label, str) or not self.ADD_LABEL[provider].match(label):
            raise QuotaError('INVALID_LABEL')
        with self.lock:
            if self.enrolling:
                raise QuotaError('ENROLLMENT_IN_PROGRESS')
            busy = (self.switching if provider == 'gemini' else self.codex_state['switching'] if provider == 'codex'
                    else self.claude_state['switching'])
            if busy:
                raise QuotaError('SWITCH_BUSY')
            live = {'gemini': self.active, 'codex': self.codex_state['active'], 'claude': self.claude_state['active']}[provider]
            if label == live:
                raise QuotaError('ACCOUNT_IN_USE')
        self.adder.remove(provider, label)
        with self.lock:
            if provider == 'gemini':
                self.rows.pop(label, None)
                if label in self.config['participants']:
                    self.config['participants'] = [p for p in self.config['participants'] if p != label]
                    try:
                        atomic_json(self.config_path, self.config)
                    except OSError:
                        pass
                    self.revision += 1
                self.event('account_removed', selected=label)
            else:
                state = self.codex_state if provider == 'codex' else self.claude_state
                state['profiles'].pop(label, None)
                self.event(f'{provider}_account_removed', selected=label)
            self.refresh_requested.set()
            return self.status()

    def check_enrollment(self):
        with self.lock:
            state = self.enrolling
        if not state or state['provider'] != 'codex' or state.get('stage') != 'waiting_device':
            return
        try:
            result = self.adder.status('codex')
        except QuotaError:
            return
        with self.lock:
            if result.get('state') == 'done':
                self.account_added('codex', result.get('enrolled') or state['label'])
            elif result.get('state') in ('failed', 'none'):
                self.enrolling = None
                self.event('account_add_failed', provider='codex', project=state['label'], error=result.get('error'))

    ACTIVITY_HOLD = 20  # seconds; display only, so gaps between short jobs do not flicker to idle

    def agy_closed(self, items):
        """Idle interactive agy sessions were closed for a switch: tell how to continue each conversation."""
        with self.lock:
            for item in items:
                project = os.path.basename((item.get('cwd') or '').rstrip('/')) or item.get('cwd')
                command = f"cd {shlex.quote(item['cwd'])} && {item['command']}"
                self.event('agy_closed', project=project, command=command)
                if self.config['notify']:
                    self.notifier(f"轉帳號時關咗 {project} 嘅 agy，用呢句接返個對話：{command}")

    def own_agy(self, fn, *args, **kwargs):
        """Run a broker call that runs agy itself; its conversation writes must not read as the user's work."""
        try:
            return fn(*args, **kwargs)
        finally:
            self.own_agy_until = time.time()

    def update_activity(self):
        activity, now = {}, time.time()
        seen = getattr(self, '_last_working', {})
        for key, probe in (('gemini', self.busy), ('codex', self.watch_io.codex_working),
                           ('claude', self.watch_io.claude_working)):
            try:
                reasons = list(probe())
            except Exception:
                activity[key] = {'working': None, 'reasons': [], 'checked_at': stamp()}
                continue
            if reasons:
                seen[key] = now
            held = not reasons and now - seen.get(key, float('-inf')) < self.ACTIVITY_HOLD
            activity[key] = {'working': bool(reasons) or held, 'reasons': reasons[:5] or (['recent'] if held else []),
                             'checked_at': stamp()}
        self._last_working = seen
        with self.lock:
            # A live account at its limit is not working, whatever its agy / Codex still does: it only hits the wall.
            spent = {'gemini': gemini_spent(self.rows.get(self.active), self.config['family']),
                     'codex': codex_exhausted(self.codex_state['profiles'].get(self.codex_state['active']) or {}),
                     'claude': codex_exhausted(self.claude_state['profiles'].get(self.claude_state['active']) or {})}
            for key, used_up in spent.items():
                if used_up and key in activity:
                    activity[key] = {'working': False, 'exhausted': True, 'reasons': ['exhausted'],
                                     'checked_at': activity[key]['checked_at']}
            self.activity = activity

    def codex_watch_add(self, params):
        """Registered by codex-account continue right after it resumes a thread in tmux."""
        thread, cwd, target = params.get('thread'), params.get('cwd'), params.get('tmux')
        if (not isinstance(thread, str) or not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', thread)
                or not isinstance(cwd, str) or not isinstance(target, str)
                or not re.fullmatch(r'[\w.-]+:[\w.-]+', target)):
            raise QuotaError('INVALID_WATCH')
        try:  # only turns written after this point belong to the resume (not the one that just ran out)
            path = self.watch_io.rollout(thread)
            offset = self.watch_io.size(path) if path else 0
        except OSError:
            offset = 0
        with self.lock:
            self.codex_watches = [w for w in self.codex_watches if not (w['thread'] == thread and w['status'] == 'running')]
            self.codex_watches.append({'thread': thread, 'project': os.path.basename(cwd.rstrip('/')) or cwd,
                                       'tmux': target, 'status': 'running', 'started_at': stamp(),
                                       'finished_at': None, 'summary': None, 'released': False, 'offset': offset})
            self.codex_watches = self.codex_watches[-20:]
            self.save_watches()
            self.event('codex_continue_started', project=self.codex_watches[-1]['project'])
            return self.status()

    def save_watches(self):
        try:
            atomic_json(self.root / 'codex_watches.json', self.codex_watches)
        except OSError:
            pass

    def check_codex_watches(self):
        """Finished → release the CLI (writer lock) and notify; CLI gone early → interrupted."""
        with self.lock:
            running = [w for w in self.codex_watches if w['status'] == 'running']
        for w in running:
            io = self.watch_io
            path = io.rollout(w['thread'])
            last, message = io.turn(path, w.get('offset', 0)) if path else (None, None)
            alive = io.cli_alive(w['thread'])
            if alive and (last not in ('task_complete', 'task_failed', 'task_error') or io.cli_busy(w['tmux'])):
                continue
            if alive:
                io.release(w['tmux'])
            done = last == 'task_complete' and bool(message)  # only an answer counts as continued
            failed = last == 'task_error'  # stopped on an error that is not the usage limit (model at capacity, ...)
            limited = not done and last in ('task_complete', 'task_failed')  # ended without an answer
            error = io.turn_error(path) if failed else None
            summary = next((line.strip() for line in (message or '').splitlines() if line.strip()), None)
            with self.lock:
                if failed:  # reported here; the error scan must not report the same stop again
                    self.session_errors[f"codex:{w['thread']}:{io.stopped_at(path)}"] = time.time()
                    self.save_session_errors()
                w.update(status='done' if done else 'error' if failed else 'limited' if limited else 'interrupted',
                         finished_at=stamp(), summary=summary[:160] if summary else None,
                         released=not io.cli_alive(w['thread']))
                self.save_watches()
                self.event('codex_continue_done' if done else 'codex_continue_error' if failed
                           else 'codex_continue_limited' if limited else 'codex_continue_interrupted',
                           project=w['project'], summary=w['summary'], **({'error': error} if failed else {}))
                if self.config['notify']:
                    self.notifier(f"Codex 接續完成：{w['project']}。已釋放對話，可以喺 app 撳重試。" if done else
                                  f"Codex 接續出錯停咗：{w['project']}（{error or '未知錯誤'}），工作未完成。" if failed else
                                  f"Codex 接續冇回應：{w['project']}（多數係撞到額度上限），工作未完成。" if limited else
                                  f"Codex 接續中斷：{w['project']}（CLI 已結束，工作未完成）。")

    def save_codex_settings(self):
        atomic_json(self.root / 'codex.json', {k: self.codex_state[k] for k in ('auto', 'autocontinue', 'waitcontinue')})

    def codex_waitcontinue_set(self, value):
        if type(value) is not bool:
            raise QuotaError('INVALID_ENABLED')
        with self.lock:
            self.codex_state['waitcontinue'] = value
            if not value:
                self.codex_waiting = []
                self.save_waiting()
            self.save_codex_settings()
            self.codex_all_exhausted = False
            self.event('codex_waitcontinue_changed', enabled=value)
            return self.status()

    def codex_autocontinue_set(self, value):
        if type(value) is not bool:
            raise QuotaError('INVALID_ENABLED')
        with self.lock:
            self.codex_state['autocontinue'] = value
            if value and not self.codex_state['auto']:  # it only ever runs after an automatic switch
                self.codex_state['auto'] = True
                self.codex_all_exhausted = False
                self.event('codex_auto_changed', enabled=True)
            self.save_codex_settings()
            self.event('codex_autocontinue_changed', enabled=value)
            return self.status()

    def warmup_set(self, params):
        provider, value = params.get('provider'), params.get('enabled')
        if provider not in self.warmup or type(value) is not bool:
            raise QuotaError('INVALID_PARAMS')
        with self.lock:
            self.warmup[provider] = value
            atomic_json(self.root / 'warmup.json', self.warmup)
            self.event(f'{provider}_warmup_changed' if provider != 'gemini' else 'warmup_changed', enabled=value)
            return self.status()

    def warm_candidates(self, now):
        found = []
        with self.lock:
            if self.warmup['gemini'] and not (self.switching or self.pending or self.enrolling):
                for label in self.config['participants']:
                    row = self.rows.get(label)
                    wins = gemini_windows(row, self.config['family'])
                    # the live account too: idle, its clock is not running either (busy is checked before warming)
                    if (row and row.get('status') == 'OK' and wins
                            and not (isinstance(wins['weekly'][0], (int, float)) and wins['weekly'][0] <= 0)):
                        found.append(('gemini', label, clock_idle(*wins['5h'], now)))
            if self.warmup['codex'] and not (self.codex_state['switching'] or self.enrolling):
                live_busy = None
                for label, r in self.codex_state['profiles'].items():
                    if label == self.codex_state['active']:
                        if live_busy is None:
                            try:
                                live_busy = bool(self.watch_io.codex_working())
                            except Exception:
                                live_busy = True
                        if live_busy:
                            continue  # a turn is running on it: it is counting already
                    w = r.get('5h') or {}
                    if r.get('status') == 'OK' and (r.get('weekly') or {}).get('remaining_percent', 1) > 0:
                        found.append(('codex', label,
                                      clock_idle(w.get('remaining_percent'), reset_epoch(w.get('reset_at')), now)))
            if self.warmup['claude'] and not self.enrolling:
                for label, r in self.claude_state['profiles'].items():
                    w = r.get('5h') or {}
                    if (r.get('status') == 'OK' and not r.get('stale')
                            and (r.get('weekly') or {}).get('remaining_percent', 1) > 0):
                        found.append(('claude', label, claude_clock_idle(w.get('remaining_percent'),
                                                                         reset_epoch(w.get('reset_at')), now)))
        return found

    def warm_idle(self, now=None):
        """At most one warm-up per provider per poll; Gemini only while agy is idle."""
        now = time.time() if now is None else now
        done = set()
        for provider, label, idle in self.warm_candidates(now):
            key = (provider, label)
            if not idle:
                self.warm_seen.pop(key, None)
                continue
            first = self.warm_seen.setdefault(key, now)
            if provider in done or now - first < WARM_DELAY or now - self.warm_last.get(key, 0) < WARM_RETRY:
                continue
            if provider == 'gemini' and self.busy():
                continue
            prefix = '' if provider == 'gemini' else provider + '_'
            try:
                if provider == 'gemini':
                    if not self.switch_lock.acquire(blocking=False):
                        continue
                    try:
                        self.own_agy(self.broker.warm, label)
                    finally:
                        self.switch_lock.release()
                elif provider == 'claude':
                    self.claude.warm(label)
                else:
                    self.codex.warm(label)
            except QuotaError as e:
                if str(e) == 'WARM_BUSY':
                    continue  # agy started meanwhile; try again next poll
                self.warm_last[key] = now
                with self.lock:
                    self.event(prefix + 'warm_failed', selected=label, error=str(e))
                continue
            self.warm_last[key] = now
            done.add(provider)
            with self.lock:
                self.event(prefix + 'warmed', selected=label)

    def provider_reset(self, params):
        """The 重設 button of a tab: drop stuck switch / retry / back-off state, then close everything of that tool
        and switch to its first account (by name), as a manual switch does. Gemini closes all agy (force: also past
        one that will not quit); Codex closes ChatGPT + Codex CLI and reopens ChatGPT; Claude Code reads the new
        login by itself (~30 s), so its sessions are not closed."""
        provider = params.get('provider')
        if provider not in ('gemini', 'codex', 'claude'):
            raise QuotaError('INVALID_PARAMS')
        with self.lock:
            busy = {'gemini': self.switching, 'codex': self.codex_state['switching'], 'claude': self.claude_state['switching']}
            if busy[provider] or (self.enrolling and self.enrolling['provider'] == provider):
                raise QuotaError('SWITCH_BUSY')
            if provider == 'gemini':
                labels = sorted(self.profiles.list())
                self.pending, self.cancelled_until, self.recovery_required = None, 0.0, False
                if (self.transaction or {}).get('phase') in ('FAILED', 'UNKNOWN', 'STARTED'):
                    self.transaction = {**self.transaction, 'phase': 'RESET'}
                self.persist()
            elif provider == 'codex':
                labels = sorted(self.codex_state['profiles'])
                self.last_codex_switch, self.codex_all_exhausted = 0.0, False
                self.codex_state['error'] = None
            else:
                labels = sorted(self.claude_state['profiles'])
                self.claude_next_poll, self.claude_interval, self.claude_good = 0.0, 0, 0
                self.claude_state['error'] = None
            if not labels:
                raise QuotaError('UNKNOWN_PROFILE')
            self.event('reset' if provider == 'gemini' else f'{provider}_reset', selected=labels[0])
        if provider == 'gemini':
            self.manual_use({'label': labels[0], 'now': True}, force=True)
        elif provider == 'codex':
            try:
                self.codex_use({'label': labels[0]}, source='reset')
            finally:
                self.codex_app_safety_net(True)
        else:
            self.claude_use({'label': labels[0]}, source='reset')
        with self.lock:
            self.refresh_scope = provider
            self.refresh_requested.set()
            return self.status()

    def codex_auto_set(self, value):
        if type(value) is not bool:
            raise QuotaError('INVALID_ENABLED')
        with self.lock:
            self.codex_state['auto'] = value
            self.save_codex_settings()
            self.codex_all_exhausted = False
            self.event('codex_auto_changed', enabled=value)
            return self.status()

    def codex_use(self, params, source='manual'):
        label = params.get('label')
        with self.lock:
            if not isinstance(label, str) or label not in self.codex_state['profiles']:
                raise QuotaError('UNKNOWN_PROFILE')
        if not self.codex_lock.acquire(blocking=False):
            raise QuotaError('SWITCH_BUSY')
        try:
            with self.lock:
                self.codex_state['switching'] = True
                self.last_codex_switch = time.time()  # also after failures: never SIGTERM ChatGPT every poll
            result = self.codex.use(label)
            with self.lock:
                self.codex_state['active'] = result['selected']
                for name, r in self.codex_state['profiles'].items():
                    r['active'] = name == result['selected']
                self.event('codex_switched', selected=result['selected'], source=source)
                if source == 'manual' and self.codex_waiting:  # the user took over: nothing automatic follows
                    self.event('codex_waiting_cleared', count=len(self.codex_waiting))
                    self.codex_waiting = []
                    self.save_waiting()
            return result
        except QuotaError as e:
            with self.lock:
                self.event('codex_switch_failed', error=str(e))
            raise
        finally:
            with self.lock:
                self.codex_state['switching'] = False
            self.codex_lock.release()
            self.refresh_requested.set()

    def gemini_app_use(self, params):
        """The Gemini Mac app onto another of its signed-in accounts (closes and reopens it; see gemini_app)."""
        account = params.get('account')
        if not isinstance(account, str):
            raise QuotaError('INVALID_PARAMS')
        if not self.gemini_app_lock.acquire(blocking=False):
            raise QuotaError('SWITCH_BUSY')
        try:
            result = self.gemini_app.use(account)
        except QuotaError as e:
            with self.lock:
                self.event('gemini_app_failed', error=str(e))
            raise
        finally:
            self.gemini_app_lock.release()
        with self.lock:
            label = next((a['label'] for a in result['accounts'] if a['id'] == account), None)
            self.event('gemini_app_switched', selected=label or account)
        return result

    def codex_reset_use(self, params):
        """Use one of an account's free usage resets (pressed in 帳號管理). Nothing automatic follows."""
        label = params.get('label')
        with self.lock:
            r = self.codex_state['profiles'].get(label) if isinstance(label, str) else None
            if r is None:
                raise QuotaError('UNKNOWN_PROFILE')
            if not r.get('resets'):
                raise QuotaError('NO_RESET_CREDIT')
        if not self.codex_lock.acquire(blocking=False):
            raise QuotaError('SWITCH_BUSY')
        try:
            result = self.codex.reset(label)
            with self.lock:
                self.event('codex_reset_used', selected=label, code=result['code'])
            return result
        except QuotaError as e:
            with self.lock:
                self.event('codex_reset_failed', selected=label, error=str(e))
            raise
        finally:
            self.codex_lock.release()
            self.refresh_requested.set()

    def poll_loop(self):
        # The master rotation switch NEVER gates this loop.
        while not self.stop.is_set():
            started = time.monotonic()
            self.refresh_requested.clear()
            scope, self.refresh_scope = self.refresh_scope, None
            self.refresh(scope)
            with self.lock:
                delay = max(1, self.config['poll_seconds'] - (time.monotonic() - started))
                refill = self.codex_refill_in(time.time())
                early = refill is not None and refill < delay  # threads wait for it: look right after it resets
            if not self.refresh_requested.wait(refill if early else delay) and early:
                self.refresh_scope = 'codex'

    def codex_refill_in(self, now, settle=10):
        """Seconds until just after the live Codex account's 5 h window resets, while threads wait for quota."""
        if not (self.codex_waiting and self.codex_state['waitcontinue']):
            return None
        row = self.codex_state['profiles'].get(self.codex_state['active']) or {}
        reset = reset_epoch((row.get('5h') or {}).get('reset_at'))
        if not reset or not codex_exhausted(row) or reset + settle <= now:
            return None  # past it already: the normal poll keeps looking
        return reset + settle - now

    def decide(self, now):
        """Pure selection under lock; only fresh complete family windows qualify."""
        config = self.config
        if self.enrolling and self.enrolling['provider'] == 'gemini':
            return None
        if not config['enabled'] or self.recovery_required or now < self.paused_until:
            self.pending = None
            return None
        if (self.switching or now < self.cancelled_until or
            now - self.last_switch < config['cooldown_seconds']):
            return None
        local = datetime.fromtimestamp(now)
        slot = local.strftime('%Y-%m-%dT%H:%M%z')
        scheduled = config['time_enabled'] and local.minute in config['hourly_minutes'] and slot != self.last_slot
        current = metrics(self.rows.get(self.active), config, now)
        if not current:
            self.pending = None
            return None
        if self.active not in config['participants']:
            self.pending = None
            return None
        live = gemini_windows(self.rows.get(self.active), config['family'])
        live_weekly = live['weekly'][0] if live else None
        # Live quota lost within 6 h is burned: only its 5 h window (or the weekly running out) moves us on.
        burning = bool(live) and urgent(live_weekly, live['weekly'][1], now)
        live_five = live['5h'][0] if live else None
        low = config['usage_enabled'] and ((isinstance(live_five, (int, float)) and live_five < config['remaining_below'])
                                           if burning else min(current) < config['remaining_below'])
        normal = self.tiers(now)[0]
        park = (config['usage_enabled'] and not low and not burning and isinstance(live_weekly, (int, float))
                and live_weekly < PRIORITY_HEADROOM and bool(normal))
        # Absolute priority: another account's weekly quota is lost within 6 h. Switch once agy is idle.
        # The live account may be burning too: move only to quota that is lost even earlier.
        rush = (config['usage_enabled'] and not (low or park) and bool(normal) and self.urgent_label(normal[0][0], now)
                and (not burning or self.weekly_reset(normal[0][0]) < live['weekly'][1]))
        if self.pending and self.pending['reason'] in ('usage', 'reserve', 'urgent') and not (low or park or rush):
            self.pending = None
        if not self.pending and (low or park or rush or scheduled):
            self.pending = {'reason': 'usage' if low else 'reserve' if park else 'urgent' if rush else 'time',
                            'slot': slot,
                            'state': 'PENDING', 'revision': self.revision}
        if not self.pending:
            return None
        if self.pending['reason'] == 'usage':
            live = gemini_windows(self.rows.get(self.active), config['family'])
            refill = live and live['5h'][1]
            if refill and 0 < refill - now <= STAY_WINDOW:
                self.pending.pop('deadline', None)
                self.pending['state'] = 'WAITING_FOR_RESET'
                return None
        candidates = self.ranked(now)
        if not candidates:
            self.pending.pop('deadline', None)
            eligible = [metrics(self.rows.get(label), config, now) for label in config['participants']]
            self.pending['state'] = ('ALL_EXHAUSTED' if eligible and all(v is not None and min(v) == 0 for v in eligible)
                                     else 'NO_READY_PROFILE')
            return None
        target, self.pending['why'] = candidates[0]
        if self.pending.get('target') != target:
            self.pending.pop('deadline', None)
            self.pending['target'] = target
        return target

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            manual = self.pending if self.pending and self.pending.get('reason') == 'manual' else None
            if manual:
                # Waits for idle regardless of quota state; never closes agy or the app.
                if self.switching or self.recovery_required or self.busy():
                    manual['state'] = 'WAITING_FOR_IDLE'
                    return
                self.pending = None
        if manual:
            try:
                self.manual_use({'label': manual['target'], 'now': True})  # idle, so closing interrupts no work
            except QuotaError as e:
                if str(e) in ('AGY_RUNNING_NOT_SWITCHED', 'SWITCH_BUSY'):
                    with self.lock:
                        self.pending = self.pending or manual  # raced with a new agy; keep waiting
            return
        with self.lock:
            target = self.decide(now)
            if not target or now - self.last_auto_attempt < 30:
                return
            if self.config['mode'] == 'notify':
                if self.pending['state'] != 'NOTIFY_ONLY':
                    self.event('rotation_recommended', selected=target)
                    if not self.pending:
                        return
                self.pending['state'] = 'NOTIFY_ONLY'
                return
            close_all = True  # idle: open-but-quiet agy/app are closed, app reopened
            values = metrics(self.rows.get(self.active), self.config, now)
            exhausted = values is not None and min(values) == 0  # nothing can work on it: switch regardless
            busy = self.busy()
            if busy:
                # Parking a < 15 % account also counts down: a never-idle worker would otherwise drain it to 0.
                if not exhausted and self.pending.get('reason') != 'reserve':
                    self.pending['state'] = 'WAITING_FOR_IDLE'
                    self.pending.pop('deadline', None)
                    return
                if not self.config['close_app']:
                    self.pending['state'] = 'WAITING_FOR_IDLE'
                    self.pending.pop('deadline', None)
                    return
                if 'deadline' not in self.pending:
                    self.pending['deadline'] = now + self.config['countdown_seconds']
                    self.event('park_countdown_started' if self.pending.get('reason') == 'reserve'
                               else 'countdown_started', selected=target)
                    if not self.pending:
                        return
                self.pending['state'] = 'COUNTDOWN'
                if now < self.pending['deadline']:
                    return
                close_all = True
            else:
                self.pending.pop('deadline', None)
            revision = self.revision
        if not self.switch_lock.acquire(blocking=False):
            return
        try:
            # Re-check master/revision after acquiring transaction ownership.
            with self.lock:
                if not self.config['enabled'] or self.revision != revision:
                    return
                if not self.pending or self.pending.get('target') != target or now < self.cancelled_until:
                    return
                if self.profiles.active() != self.active:
                    self.pending = None
                    self.refresh_requested.set()
                    self.event('drift')
                    return
                self.last_auto_attempt = now
                slot = self.pending['slot']
                reason = self.pending.get('why')
                self.switching = True
                self.pending = None
                self.transaction = {'phase': 'STARTED', 'selected': target, 'previous': self.active,
                                    'source': 'automatic', 'time': stamp()}
                self.event('switch_started', selected=target, source='automatic', why=reason)
                if not self.config['enabled']:
                    return
            try:
                result = self.own_agy(self.broker.switch, target, close_all=close_all,
                                      **({'force': True} if exhausted else {}))
                with self.lock:
                    self.active = result['selected']
                    self.last_switch = now
                    self.last_slot = slot
                    self.transaction['phase'] = 'COMPLETE'
                    self.persist()
                    self.event('switched', selected=self.active, source='automatic', why=reason)
            except QuotaError as e:
                with self.lock:
                    self.transaction['phase'] = 'FAILED' if str(e) in NOT_SWITCHED else 'UNKNOWN'
                    self.recovery_required = self.transaction['phase'] == 'UNKNOWN'
                    self.cancelled_until = now + RETRY_AFTER.get(str(e), self.config['cooldown_seconds'])
                    self.event('switch_failed', error=str(e))
            except Exception:
                with self.lock:
                    self.transaction['phase'] = 'UNKNOWN'
                    self.recovery_required = True
                    self.event('switch_failed', error='SWITCH_STATE_UNKNOWN')
        finally:
            with self.lock:
                self.switching = False
                self.refresh_requested.set()
            self.switch_lock.release()

    def policy_loop(self):
        while not self.stop.wait(5):
            try:
                self.check_codex_watches()
            except Exception:
                pass  # a broken watch must never stop rotation
            self.update_activity()
            try:
                self.check_enrollment()
            except Exception:
                pass
            try:
                self.tick()
            except Exception:
                with self.lock:
                    self.event('policy_error', error='INTERNAL_ERROR')

    def manual_use(self, params, force=False):
        label = params.get('label')
        if not isinstance(label, str) or label not in self.profiles.list():
            raise QuotaError('UNKNOWN_PROFILE')
        if self.enrolling and self.enrolling['provider'] == 'gemini':
            raise QuotaError('ENROLLMENT_IN_PROGRESS')
        close_all = params.get('now', False)  # a used-up target is allowed: a manual switch is the user's explicit choice
        wait_idle = params.get('wait_idle', False)
        if type(close_all) is not bool or type(wait_idle) is not bool or (close_all and wait_idle):
            raise QuotaError('INVALID_NOW')
        if wait_idle:
            with self.lock:
                self.pending = {'reason': 'manual', 'target': label, 'state': 'WAITING_FOR_IDLE',
                                'slot': None, 'revision': self.revision}
                self.event('manual_queued', selected=label)
                return self.status()
        if not self.switch_lock.acquire(blocking=False):
            raise QuotaError('SWITCH_BUSY')
        try:
            with self.lock:
                self.switching = True
                self.transaction = {'phase': 'STARTED', 'selected': label, 'previous': self.active,
                                    'source': 'manual', 'time': stamp()}
                self.persist()
                if self.persistence_error:
                    raise QuotaError('STATE_WRITE_FAILED_AUTOMATION_DISABLED')
            result = self.own_agy(self.broker.switch, label, close_all=close_all, **({'force': True} if force else {}))
            with self.lock:
                self.active = result['selected']
                self.last_switch = time.time()
                self.pending = None
                self.transaction['phase'] = 'COMPLETE'
                self.recovery_required = False
                self.persist()
                self.event('switched', selected=self.active, source='manual')
                self.refresh_requested.set()
            return result
        except QuotaError as e:
            with self.lock:
                self.active = self.profiles.active()
                self.transaction['phase'] = 'FAILED' if str(e) in NOT_SWITCHED else 'UNKNOWN'
                self.recovery_required = self.transaction['phase'] == 'UNKNOWN'
                self.event('switch_failed', error=str(e))
                self.refresh_requested.set()
            raise
        except Exception:
            with self.lock:
                self.transaction['phase'] = 'UNKNOWN'
                self.recovery_required = True
                self.event('switch_failed', error='SWITCH_STATE_UNKNOWN')
            raise QuotaError('SWITCH_STATE_UNKNOWN') from None
        finally:
            with self.lock:
                self.switching = False
            self.switch_lock.release()

    def call(self, method, params):
        if not isinstance(params, dict):
            raise QuotaError('INVALID_PARAMS')
        if method in ('status', 'list', 'current', 'quota', 'config.get'):
            return self.status()
        if method == 'refresh':
            scope = params.get('provider')
            if scope not in (None, 'gemini', 'codex', 'claude'):
                raise QuotaError('INVALID_PARAMS')
            self.refresh_scope = scope  # a manual refresh updates only the tab that asked
            self.refresh_requested.set()
            return {'accepted': True}
        if method == 'enabled.set':
            return self.set_enabled(params.get('enabled'))
        if method == 'config.set':
            return self.configure(params)
        if method == 'config.defaults':
            return {**copy.deepcopy(DEFAULTS), 'participants': list(self.profiles.list())}
        if method in ('pause', 'resume'):
            seconds = params.get('seconds', 1800) if method == 'pause' else 0
            if type(seconds) is not int or (method == 'pause' and not 30 <= seconds <= 86400):
                raise QuotaError('INVALID_PAUSE_DURATION')
            with self.lock:
                self.paused_until = time.time() + seconds if seconds else 0
                self.drop_auto_pending()
                self.event('paused' if seconds else 'resumed')
                return self.status()
        if method == 'recovery.ack':
            with self.lock:
                if self.switching:
                    raise QuotaError('SWITCH_BUSY')
                active = self.profiles.active()
                if active is None:
                    raise QuotaError('LIVE_IDENTITY_UNKNOWN')
                self.active = active
                self.recovery_required = False
                self.last_switch = time.time()
                self.transaction = {'phase': 'ACKNOWLEDGED', 'selected': active, 'time': stamp()}
                self.event('recovery_acknowledged', selected=active)
                return self.status()
        if method == 'cancel':
            with self.lock:
                if self.pending and self.pending.get('reason') == 'manual':
                    self.pending = None
                    self.event('manual_cancelled')
                    return self.status()
                self.pending = None
                self.cancelled_until = time.time() + self.config['cooldown_seconds']
                self.persist()
                self.event('rotation_cancelled')
                return self.status()
        if method == 'use':
            return self.manual_use(params)
        if method == 'provider.reset':
            return self.provider_reset(params)
        if method == 'claude.use':
            self.claude_use(params)
            return self.status()
        if method == 'codex.use':
            return self.codex_use(params)
        if method == 'codex.reset.use':
            return self.codex_reset_use(params)
        if method == 'geminiapp.state':
            return self.gemini_app.state()
        if method == 'geminiapp.use':
            return self.gemini_app_use(params)
        if method == 'accounts.add.start':
            return self.account_add_start(params)
        if method == 'accounts.add.finish':
            return self.account_add_finish(params)
        if method == 'accounts.add.cancel':
            return self.account_add_cancel()
        if method == 'accounts.remove':
            return self.account_remove(params)
        if method == 'codex.watch.add':
            return self.codex_watch_add(params)
        if method == 'claude.waitcontinue.set':
            return self.claude_waitcontinue_set(params.get('enabled'))
        if method == 'continue.preview':
            return self.continue_preview(params.get('provider'), params.get('session'))
        if method == 'continue.sessions':
            return self.continue_sessions(params.get('provider'))
        if method == 'agy.continue.now':
            return self.agy_continue_now(params.get('text'), params.get('session'))
        if method == 'claude.continue.now':
            return self.claude_continue_now(params.get('text'), params.get('session'))
        if method == 'codex.continue.now':
            return self.codex_continue_now(params.get('session') or params.get('thread'), params.get('text'))
        if method == 'codex.waitcontinue.set':
            return self.codex_waitcontinue_set(params.get('enabled'))
        if method == 'codex.autocontinue.set':
            return self.codex_autocontinue_set(params.get('enabled'))
        if method == 'codex.auto.set':
            return self.codex_auto_set(params.get('enabled'))
        if method == 'warmup.set':
            return self.warmup_set(params)
        raise QuotaError('METHOD_NOT_FOUND')


def serve(root, service_factory=Service):
    os.umask(0o077)
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Acquire before reading/writing state or unlinking the socket.
    lock = open(root / 'daemon.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise QuotaError('DAEMON_ALREADY_RUNNING') from None
    service = service_factory(root, notifier=notify_macos)
    path = root / 'rotatord.sock'
    if path.exists():
        path.unlink()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(90)
            ident = None
            try:
                line = self.rfile.readline(262145)
                if len(line) > 262144:
                    raise QuotaError('REQUEST_TOO_LARGE')
                req = json.loads(line)
                if not isinstance(req, dict) or req.get('jsonrpc') != '2.0':
                    raise QuotaError('INVALID_REQUEST')
                ident = req.get('id')
                if type(ident) not in (int, str, type(None)):
                    raise QuotaError('INVALID_REQUEST_ID')
                result = service.call(req.get('method'), req.get('params', {}))
                response = {'jsonrpc': '2.0', 'id': ident, 'result': result}
            except Exception as e:
                response = {'jsonrpc': '2.0', 'id': ident, 'error': {
                    'code': -32000, 'message': str(e) if isinstance(e, QuotaError) else 'INVALID_REQUEST'}}
            try:
                self.wfile.write(json.dumps(response).encode() + b'\n')
            except OSError:
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        def handle_error(self, request, client_address):
            pass  # Never emit a traceback containing request/credential locals.

    with Server(str(path), Handler) as server:
        os.chmod(path, 0o600)
        threading.Thread(target=service.poll_loop, daemon=True).start()
        threading.Thread(target=service.policy_loop, daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            service.stop.set()
            service.refresh_requested.set()
            path.unlink(missing_ok=True)


def rpc(root, method, params=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(90)
        sock.connect(str(Path(root) / 'rotatord.sock'))
        sock.sendall(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method,
                                 'params': params or {}}, ensure_ascii=False).encode() + b'\n')  # CJK: 3 bytes, not 6
        with sock.makefile('rb') as f:
            result = json.loads(f.readline(1024 * 1024))
        if 'error' in result:
            raise QuotaError(result['error']['message'])
        return result['result']
