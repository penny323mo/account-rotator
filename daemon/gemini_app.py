"""The Gemini Mac app's signed-in Google account, switched by hand from the page.

The Gemini app (com.google.GeminiMacOS) keeps its own logins, separate from agy's Keychain item: one folder per
account under Data/user1..userN, and `account_config` (protobuf) naming the account in use (field 4) and every
signed-in account (field 6: {1: folder name, 2: state}). Its Code Mode runs its own bundled agy and hands it the
token of that account, so switching agy never moves it. The app reads `account_config` at launch: a switch quits
it, rewrites field 4 and opens it again (verified 2026-10-15 on Gemini 1.128 with all six accounts).
Which Google account a folder holds is not readable (the auth files are encrypted); `gemini_app.json` in the
rotator's folder maps folder names to labels, e.g. {"user2": "GEMINI_A"}."""
import json
import os
import subprocess
import time

from quota import QuotaError

BUNDLE = 'com.google.GeminiMacOS'
SUPPORT = os.path.expanduser('~/Library/Application Support/com.google.GeminiMacOS')
QUIT_WAIT = 30  # seconds for the app to close after being asked


def varint(data, i):
    value = shift = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, i


def fields(data):
    """[(number, wire type, value)] of one protobuf message (varint and length-delimited fields only)."""
    out, i = [], 0
    while i < len(data):
        key, i = varint(data, i)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, i = varint(data, i)
        elif kind == 2:
            size, i = varint(data, i)
            value, i = data[i:i + size], i + size
        else:
            raise ValueError('unexpected protobuf field')
        out.append((number, kind, value))
    return out


def encode_varint(value):
    out = bytearray()
    while True:
        byte, value = value & 0x7f, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def read_config(raw):
    """(account in use, [signed-in accounts in the app's order])."""
    active, accounts = None, []
    for number, kind, value in fields(raw):
        if number == 4 and kind == 2:
            active = value.decode()
        elif number == 6 and kind == 2:
            name = next((v.decode() for n, k, v in fields(value) if n == 1 and k == 2), None)
            if name:
                accounts.append(name)
    return active, accounts


def with_active(raw, account):
    """The same config with field 4 naming `account` (every other field kept as it was)."""
    name = account.encode()
    out, done = bytearray(), False
    i = 0
    for number, kind, value in fields(raw):
        start = i
        key, i = varint(raw, i)
        if kind == 0:
            _, i = varint(raw, i)
        else:
            size, i = varint(raw, i)
            i += size
        if number == 4 and kind == 2:
            if not done:
                out += b'\x22' + encode_varint(len(name)) + name
                done = True
        else:
            out += raw[start:i]
    if not done:
        out = bytearray(b'\x22' + encode_varint(len(name)) + name) + out
    return bytes(out)


class SystemIO:
    """The real side effects; tests replace this."""

    def __init__(self, support=SUPPORT, names=None):
        self.support = support
        self.names = names

    def read(self):
        with open(os.path.join(self.support, 'account_config'), 'rb') as f:
            return f.read()

    def write(self, raw):
        path = os.path.join(self.support, 'account_config')
        tmp = path + '.rotator'
        with open(tmp, 'wb') as f:
            f.write(raw)
        os.replace(tmp, path)

    def labels(self):
        try:
            with open(self.names) as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def running(self):
        return subprocess.run(['/usr/bin/pgrep', '-f', '/Gemini.app/Contents/MacOS/Gemini$'],
                              capture_output=True, timeout=5).returncode == 0

    def quit(self):
        subprocess.run(['/usr/bin/osascript', '-e', f'tell application id "{BUNDLE}" to quit'],
                       capture_output=True, timeout=20)

    def open(self):
        subprocess.run(['/usr/bin/open', '-b', BUNDLE], capture_output=True, timeout=20, check=True)

    def sleep(self, seconds):
        time.sleep(seconds)


class GeminiApp:
    def __init__(self, io=None):
        self.io = io or SystemIO()

    def state(self):
        """{installed, running, active, accounts: [{id, label}]}; installed is False without the app's data."""
        try:
            active, accounts = read_config(self.io.read())
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            return {'installed': False, 'running': False, 'active': None, 'accounts': []}
        labels = self.io.labels()
        return {'installed': True, 'running': self.io.running(), 'active': active,
                'accounts': [{'id': a, 'label': labels.get(a)} for a in accounts]}

    def use(self, account):
        """Make `account` the one in use: the app is closed, switched and opened again (left closed if it was)."""
        try:
            raw = self.io.read()
            active, accounts = read_config(raw)
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            raise QuotaError('GEMINI_APP_NOT_FOUND')
        if account not in accounts:
            raise QuotaError('UNKNOWN_PROFILE')
        if account == active:
            return self.state()
        was_running = self.io.running()
        if was_running:
            self.io.quit()
            for _ in range(QUIT_WAIT * 2):
                if not self.io.running():
                    break
                self.io.sleep(0.5)
            else:
                raise QuotaError('GEMINI_APP_STILL_OPEN')  # nothing changed; it may be asking something
        self.io.write(with_active(self.io.read(), account))
        if was_running:
            self.io.open()
        return self.state()
