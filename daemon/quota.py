"""Account-scoped quota reads. Credentials stay in Keychain / process memory.

Protocol reference: ibravemonkey/agyp, d9141ca3390ab815bebb9c9845d2409009cfff7d,
pkg/profile/quota.go. No source code or OAuth credential literals are vendored.
"""
import base64
import hashlib
import json
import math
import re
import subprocess
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CLIENT_ID_HASH = 'bf00c418024ba6bf606ccdc37120976e41bc429dd1d46ecf16a729aa532626ea'
CLIENT_SECRET_HASH = '1d2f041093fd95aa8995a038c711d50a7960da09a505381c09a745d6ad0ecc60'  # gitleaks:allow (a SHA-256 used to recognise the secret, not the secret)
BASE = 'https://daily-cloudcode-pa.googleapis.com/v1internal:'
TOKEN_URL = 'https://oauth2.googleapis.com/token'


class QuotaError(Exception):
    """Only constant, non-sensitive error codes may cross the API boundary."""


def stamp():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0


def keychain(service, account):
    try:
        r = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', service,
                            '-a', account, '-w'], capture_output=True, timeout=10)
        if r.returncode:
            raise QuotaError('KEYCHAIN_UNAVAILABLE')
        return r.stdout.rstrip(b'\n')
    except subprocess.TimeoutExpired:
        raise QuotaError('KEYCHAIN_TIMEOUT') from None


def credential(raw):
    try:
        if raw.startswith(b'go-keyring-base64:'):
            raw = base64.b64decode(raw.split(b':', 1)[1], validate=True)
        elif raw.startswith(b'go-keyring-encoded:'):
            raw = bytes.fromhex(raw.split(b':', 1)[1].decode())
        d = json.loads(raw)
        payload = d['id_token'].split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        ident = hashlib.sha256(f"{claims['iss']}|{claims['sub']}".encode()).hexdigest()[:12]
        if not isinstance(d.get('token'), dict):
            raise ValueError()
        return d['token'], claims['aud'], ident
    except Exception:
        raise QuotaError('MALFORMED_CREDENTIAL') from None


class Profiles:
    def __init__(self, metadata=None):
        self.metadata = metadata or Path.home() / '.agy-account/profiles.json'

    def list(self):
        try:
            rows = json.loads(self.metadata.read_text())['profiles']
            if not isinstance(rows, dict) or any(
                not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', k) or
                not isinstance(v, dict) or not re.fullmatch(r'[a-f0-9]{12}', v.get('id', ''))
                for k, v in rows.items()
            ):
                raise ValueError()
            return rows
        except Exception:
            raise QuotaError('PROFILE_METADATA_UNAVAILABLE') from None

    def display_email(self, label):
        # Display-only claim from the already enrolled credential, identity checked.
        try:
            raw = keychain('agy-account-broker', label)
            if credential(raw)[2] != self.list()[label]['id']:
                return None
            if raw.startswith(b'go-keyring-base64:'):
                raw = base64.b64decode(raw.split(b':', 1)[1], validate=True)
            elif raw.startswith(b'go-keyring-encoded:'):
                raw = bytes.fromhex(raw.split(b':', 1)[1].decode())
            payload = json.loads(raw)['id_token'].split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            email = claims.get('email')
            return email if isinstance(email, str) and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) and len(email) <= 254 else None
        except Exception:
            return None

    def active(self):
        try:
            _, _, ident = credential(keychain('gemini', 'antigravity'))
            return next((k for k, v in self.list().items() if v['id'] == ident), None)
        except QuotaError:
            return None

    def read(self, label):
        rows = self.list()
        if label not in rows:
            raise QuotaError('UNKNOWN_PROFILE')
        token, audience, ident = credential(keychain('agy-account-broker', label))
        if rows[label]['id'] != ident:
            raise QuotaError('PROFILE_IDENTITY_MISMATCH')
        return token, audience, ident


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization or refresh credentials to a redirected host.
        return None


def request(url, data, access=None):
    if url not in (TOKEN_URL, BASE + 'loadCodeAssist', BASE + 'retrieveUserQuotaSummary'):
        raise QuotaError('ENDPOINT_NOT_ALLOWED')
    form = url == TOKEN_URL
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json',
               'User-Agent': 'antigravity'}
    if access:
        headers['Authorization'] = 'Bearer ' + access
    try:
        # Direct TLS to Google; no environment proxy gets account credentials.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(urllib.request.Request(url, body, headers), timeout=15) as r:
            raw = r.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise QuotaError('RESPONSE_TOO_LARGE')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise QuotaError('INVALID_RESPONSE')
            return result
    except urllib.error.HTTPError as e:
        # Do not include the request, headers, response body, or exception text.
        raise QuotaError(('TOKEN_' if form else 'QUOTA_') + f'HTTP_{e.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise QuotaError('NETWORK_UNAVAILABLE') from None
    except (ValueError, UnicodeError):
        raise QuotaError('INVALID_RESPONSE') from None


def normalize(data):
    """Missing fractions are unknown, never implicit zero or 100 percent."""
    result = []
    groups = data.get('groups')
    if not isinstance(groups, list):
        raise QuotaError('UNSUPPORTED_QUOTA_SCHEMA')
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get('displayName', ''))
        family = 'gemini' if 'gemini' in name.lower() else (
            'claude_gpt' if any(x in name.lower() for x in ('claude', 'gpt')) else None)
        if not family:
            continue
        buckets = group.get('buckets', [])
        if not isinstance(buckets, list):
            raise QuotaError('UNSUPPORTED_QUOTA_SCHEMA')
        windows = {}
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            window = bucket.get('window')
            if window not in ('5h', 'weekly'):
                continue
            if window in windows:
                raise QuotaError('AMBIGUOUS_QUOTA_SCHEMA')
            fraction = bucket.get('remainingFraction')
            valid = (type(fraction) in (int, float) and math.isfinite(fraction)
                     and 0 <= fraction <= 1)
            reset = bucket.get('resetTime')
            windows[window] = {
                'remaining_percent': fraction * 100 if valid else None,
                'reset_at': reset if timestamp(reset) else None,
            }
        if any(g['family'] == family for g in result):
            raise QuotaError('AMBIGUOUS_QUOTA_SCHEMA')
        result.append({'family': family, 'windows': windows})
    if not result or not any(b['remaining_percent'] is not None
                             for g in result for b in g['windows'].values()):
        raise QuotaError('QUOTA_UNAVAILABLE')
    return result


class QuotaClient:
    def __init__(self, profiles=None, binary=Path(shutil.which('agy') or '/opt/homebrew/bin/agy')):
        self.profiles = profiles or Profiles()
        self.binary = binary
        self.tokens = {}
        self.client_secret = None

    def oauth_client(self, audience):
        # Installed/native OAuth clients are distributed with their registration
        # metadata. Select only the version verified against the pinned source.
        # A changed CLI fails closed; no guesses, remote code, or vendored secret.
        if not isinstance(audience, str) or hashlib.sha256(audience.encode()).hexdigest() != CLIENT_ID_HASH:
            raise QuotaError('OAUTH_CLIENT_UNSUPPORTED')
        if self.client_secret is None:
            try:
                binary = self.binary.read_bytes()
            except OSError:
                raise QuotaError('AGY_BINARY_UNAVAILABLE') from None
            found = {v for v in re.findall(rb'GOCSPX-[A-Za-z0-9_-]{28}', binary)
                     if hashlib.sha256(v).hexdigest() == CLIENT_SECRET_HASH}
            if len(found) != 1 or audience.encode() not in binary:
                raise QuotaError('OAUTH_CLIENT_UNSUPPORTED')
            self.client_secret = found.pop().decode()
        return audience, self.client_secret

    def access_token(self, token, audience, ident, force=False):
        refresh = token.get('refresh_token')
        fingerprint = hashlib.sha256((refresh or '').encode()).hexdigest()
        cached = self.tokens.get(ident)
        if not force:
            if cached and cached[0] == fingerprint and cached[2] > time.time() + 120:
                return cached[1]
            if token.get('access_token') and timestamp(token.get('expiry')) > time.time() + 120:
                return token['access_token']
        if not refresh:
            raise QuotaError('LOGIN_REQUIRED')
        client_id, secret = self.oauth_client(audience)
        d = request(TOKEN_URL, {'client_id': client_id, 'client_secret': secret,
                               'grant_type': 'refresh_token', 'refresh_token': refresh})
        if not isinstance(d.get('access_token'), str) or not d['access_token']:
            raise QuotaError('INVALID_TOKEN_RESPONSE')
        # Google refresh tokens normally do not rotate on this grant. If they do,
        # do not overwrite the shared live item or silently claim persistence.
        if d.get('refresh_token') and d['refresh_token'] != refresh:
            raise QuotaError('REFRESH_TOKEN_ROTATED_RELOGIN_REQUIRED')
        lifetime = d.get('expires_in', 0)
        if type(lifetime) not in (int, float) or not 0 < lifetime <= 86400:
            raise QuotaError('INVALID_TOKEN_RESPONSE')
        self.tokens[ident] = (fingerprint, d['access_token'], time.time() + lifetime)
        return d['access_token']

    def fetch(self, label):
        token, audience, ident = self.profiles.read(label)
        for attempt in range(2):
            access = self.access_token(token, audience, ident, force=bool(attempt))
            try:
                project = request(BASE + 'loadCodeAssist',
                                  {'metadata': {'ideType': 'ANTIGRAVITY'}}, access).get('cloudaicompanionProject')
                if isinstance(project, dict):
                    project = project.get('id')
                if not isinstance(project, str) or not project:
                    raise QuotaError('PROJECT_UNAVAILABLE')
                groups = normalize(request(BASE + 'retrieveUserQuotaSummary', {'project': project}, access))
                return {'label': label, 'status': 'OK', 'updated_at': stamp(), 'groups': groups,
                        'email': self.profiles.display_email(label) if hasattr(self.profiles, 'display_email') else None}
            except QuotaError as e:
                if str(e) != 'QUOTA_HTTP_401' or attempt:
                    raise
        raise QuotaError('LOGIN_REQUIRED')


if __name__ == '__main__':
    client = QuotaClient()
    for label in client.profiles.list():
        try:
            print(json.dumps(client.fetch(label)))
        except QuotaError as error:
            print(json.dumps({'label': label, 'status': 'UNAVAILABLE', 'error': str(error)}))
