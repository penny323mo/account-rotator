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
from codex_watch import WatchIO
from claude import ClaudeAccounts
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


def transient(error):
    """Network / rate-limit / server-side errors that usually clear on the next poll (not auth problems)."""
    return (error in ('NETWORK_UNAVAILABLE', 'INVALID_RESPONSE', 'RESPONSE_TOO_LARGE', 'QUOTA_INTERNAL_ERROR')
            or error.endswith(('_HTTP_429', '_HTTP_500', '_HTTP_502', '_HTTP_503', '_HTTP_504')))


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
    values = [group['windows'].get(w, {}).get('remaining_percent') for w in config['windows']]
    return values if all(type(v) in (int, float) and 0 <= v <= 100 for v in values) else None


class Service:
    def __init__(self, root, profiles=None, client=None, broker=None, busy=None, notifier=None, codex=None, watch_io=None, adder=None, claude=None):
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
                            'auto': False, 'autocontinue': False}
        self.codex_lock = threading.Lock()
        self.watch_io = watch_io or WatchIO()
        self.claude = claude or ClaudeAccounts()
        self.claude_state = {'profiles': {}, 'active': None, 'updated_at': None, 'error': None, 'switching': False}
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
        # Is the live account busy? None = could not tell (never shown as idle).
        self.activity = {k: {'working': None, 'reasons': [], 'checked_at': None} for k in ('gemini', 'codex', 'claude')}
        try:
            saved = json.loads((self.root / 'codex.json').read_text())
            self.codex_state['auto'] = saved.get('auto') is True
            self.codex_state['autocontinue'] = saved.get('autocontinue') is True
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

    def event(self, kind, **details):
        item = {'time': stamp(), 'kind': kind, **details}
        self.events = (self.events + [item])[-100:]
        self.persist()
        messages = {'rotation_recommended': '已達輪轉條件，只通知模式未切換。',
                    'countdown_started': 'Quota 已用盡，倒數後將關閉 agy 並切換。可在 App 取消。',
                    'park_countdown_started': '每週額度低過 15%，倒數後轉去其他帳號，留低呢個做後備。可在 App 取消。',
                    'switched': '帳號切換完成。', 'switch_failed': '帳號切換失敗，請查看 App 狀態。',
                    'codex_all_exhausted': 'Codex 所有帳號都已用盡，未有自動切換。',
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
                if isinstance(five, (int, float)) and five >= config['remaining_below']:
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
        finally:
            if scope in (None, 'codex'):
                self.refresh_codex()
            if scope in (None, 'claude'):
                self.refresh_claude(force=scope == 'claude')
            with self.lock:
                self.refreshing = False
                self.last_refresh_finished = stamp()
        try:
            self.warm_idle()
        except Exception:
            pass  # warm-up is best effort; never breaks polling

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
            result = self.claude.use(label)
            with self.lock:
                self.claude_state['active'] = result['selected']
                for name, r in self.claude_state['profiles'].items():
                    r['active'] = name == result['selected']
                self.event('claude_switched', selected=result['selected'], source=source)
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

    def codex_auto(self, now):
        """Switch only when the live Codex account is used up and another one still has capacity."""
        with self.lock:
            state = self.codex_state
            if not state['auto'] or state['switching'] or now - self.last_codex_switch < CODEX_COOLDOWN:
                return
            active, rows = state['active'], state['profiles']
            if not active or not codex_exhausted(rows.get(active, {})):
                self.codex_all_exhausted = False
                return
            refill = reset_epoch((rows[active].get('5h') or {}).get('reset_at'))
            if refill and 0 < refill - now <= STAY_WINDOW:
                return  # live account refills within 15 min: do not close ChatGPT / Codex for that
            candidates = codex_order(rows, active, now, self.config['remaining_below'])
            if not candidates:
                if not self.codex_all_exhausted:
                    self.codex_all_exhausted = True
                    self.event('codex_all_exhausted')
                return
            target = candidates[0]
            autocontinue = state['autocontinue']
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
        try:
            self.codex_use({'label': target}, source='automatic')
        except QuotaError:
            return  # Recorded as codex_switch_failed; the cooldown stops retrying every poll.
        for item in captured:
            project = os.path.basename((item.get('cwd') or '').rstrip('/')) or item['thread'][-6:]
            try:
                self.codex.continue_thread(item['thread'], item.get('model'))
                with self.lock:
                    self.event('codex_autocontinue', project=project, selected=target)
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
            if alive and (last != 'task_complete' or io.cli_busy(w['tmux'])):
                continue
            if alive:
                io.release(w['tmux'])
            done = last == 'task_complete'
            summary = next((line.strip() for line in (message or '').splitlines() if line.strip()), None)
            with self.lock:
                w.update(status='done' if done else 'interrupted', finished_at=stamp(),
                         summary=summary[:160] if summary else None, released=not io.cli_alive(w['thread']))
                self.save_watches()
                self.event('codex_continue_done' if done else 'codex_continue_interrupted',
                           project=w['project'], summary=w['summary'])
                if self.config['notify']:
                    self.notifier(f"Codex 接續完成：{w['project']}。已釋放對話，可以喺 app 撳重試。" if done else
                                  f"Codex 接續中斷：{w['project']}（CLI 已結束，工作未完成）。")

    def codex_autocontinue_set(self, value):
        if type(value) is not bool:
            raise QuotaError('INVALID_ENABLED')
        with self.lock:
            self.codex_state['autocontinue'] = value
            atomic_json(self.root / 'codex.json', {'auto': self.codex_state['auto'], 'autocontinue': value})
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
            atomic_json(self.root / 'codex.json', {'auto': value, 'autocontinue': self.codex_state['autocontinue']})
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

    def poll_loop(self):
        # The master rotation switch NEVER gates this loop.
        while not self.stop.is_set():
            started = time.monotonic()
            self.refresh_requested.clear()
            scope, self.refresh_scope = self.refresh_scope, None
            self.refresh(scope)
            with self.lock:
                delay = max(1, self.config['poll_seconds'] - (time.monotonic() - started))
            self.refresh_requested.wait(delay)

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
        low = config['usage_enabled'] and (live['5h'][0] < config['remaining_below'] if burning
                                           else min(current) < config['remaining_below'])
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
                line = self.rfile.readline(65537)
                if len(line) > 65536:
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
                                 'params': params or {}}).encode() + b'\n')
        with sock.makefile('rb') as f:
            result = json.loads(f.readline(1024 * 1024))
        if 'error' in result:
            raise QuotaError(result['error']['message'])
        return result['result']
