#!/usr/bin/env python3
"""The web console and its bridge to the daemon.

Phone remote control is off by default, and then this behaves exactly as before: it listens on 127.0.0.1 only and
needs no login. Turned on (remote.json), it also listens on the addresses the user picked (Wi-Fi, Tailscale) and/or
accepts a public address's Host (ngrok, cloudflared, Tailscale Funnel). Every request that is not made on this Mac to
127.0.0.1 / localhost must then come from a paired device: the Mac shows a one-time code (QR or typed, 5 minutes,
single use); pairing sets a cookie whose hash is kept in devices.json, expires after 90 days unused, and can be
removed on the Mac at any time. A paired device may only view usage and switch / pause (no e-mail addresses are
sent to it) unless "full control" is on; pairing and devices are managed on this Mac only. No account credentials
reach this process.
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from service import rpc
from quota import QuotaError

ASSETS = Path(__file__).parent
METHODS = {'refresh', 'enabled.set', 'config.set', 'config.defaults', 'use',
           'cancel', 'pause', 'resume', 'recovery.ack', 'codex.use', 'codex.auto.set', 'codex.autocontinue.set', 'warmup.set', 'accounts.add.start', 'accounts.add.finish', 'accounts.add.cancel', 'accounts.remove', 'provider.reset', 'claude.use'}
# What a paired device may do unless full control is on.
REMOTE_METHODS = {'refresh', 'use', 'codex.use', 'claude.use', 'pause', 'resume', 'cancel'}
# Handled here, never by the daemon, and only on this Mac.
LOCAL_METHODS = {'remote.info', 'remote.set', 'pair.start', 'pair.cancel', 'devices.remove'}
COOKIE = 'agy_device'
PAIR_SECONDS = 300
DEVICE_IDLE = 90 * 86400     # a device unused this long must pair again
FAILURES = 10                # wrong codes / bad cookies per client address ...
FAILURE_WINDOW = 60          # ... per minute
CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTVWXYZ'  # no 0/O, 1/I/L, U: easy to type
HOST = re.compile(r'^[a-z0-9.-]{1,253}(:[0-9]{1,5})?$')
TAILNET = ipaddress.ip_network('100.64.0.0/10')


def wire(value):
    # Revisions are nanosecond integers: JavaScript numbers cannot round-trip them.
    if isinstance(value, dict):
        return {k: str(v) if k == 'revision' else wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [wire(v) for v in value]
    return value


def without_email(value):
    """A status for a paired device: account e-mail addresses are left out."""
    if isinstance(value, dict):
        return {k: without_email(v) for k, v in value.items() if k != 'email'}
    if isinstance(value, list):
        return [without_email(v) for v in value]
    return value


def normal_code(code):
    return re.sub(r'[\s-]', '', code or '').upper()


def device_name(agent):
    for key, name in (('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'), ('Macintosh', 'Mac'),
                      ('Windows', 'Windows')):
        if key in (agent or ''):
            return name
    return '瀏覽器'


def interface_addresses():
    """This Mac's IPv4 addresses: (private Wi-Fi / LAN ones, Tailscale ones)."""
    lan, tailnet = [], []
    try:
        out = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return lan, tailnet
    for addr in re.findall(r'inet (\d+\.\d+\.\d+\.\d+)', out):
        ip = ipaddress.ip_address(addr)
        if ip in TAILNET:
            tailnet.append(addr)
        elif ip.is_private and not ip.is_loopback and not ip.is_link_local:
            lan.append(addr)
    return list(dict.fromkeys(lan)), list(dict.fromkeys(tailnet))


def lan_addresses():
    return interface_addresses()[0]


def ngrok_hosts():
    """Public hosts of a running ngrok agent (its local API), if any."""
    try:
        with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=1) as r:
            tunnels = json.load(r).get('tunnels') or []
    except Exception:
        return []
    return [urlsplit(t.get('public_url', '')).netloc for t in tunnels if t.get('public_url', '').startswith('https://')]


class Remote:
    """Remote settings (remote.json) and paired devices (devices.json), both 0600 in the rotator's folder."""
    DEFAULTS = {'enabled': False, 'lan': False, 'tailscale': False, 'public': '', 'full': False}

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.pairing = None            # {'hash', 'expires'}: at most one code is valid at a time
        self.failures = {}             # client address -> [failure times]

    def _read(self, name):
        try:
            data = json.loads((self.root / name).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name, data):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / name
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def settings(self):
        data = self._read('remote.json')
        out = {k: data.get(k, v) if type(data.get(k, v)) is type(v) else v for k, v in self.DEFAULTS.items()}
        if out['public'] and not HOST.match(out['public']):
            out['public'] = ''
        return out

    def set(self, values):
        with self.lock:
            data = {**self.settings(), **values}
            self._write('remote.json', data)
            return data

    def devices(self):
        return self._read('devices.json').get('devices') or {}

    def list(self):
        return [{'id': k, 'name': v.get('name'), 'paired_at': v.get('paired_at'), 'last_used': v.get('last_used')}
                for k, v in sorted(self.devices().items(), key=lambda kv: kv[1].get('paired_at') or 0)]

    def start(self):
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(16))  # ~78 bits
        with self.lock:
            self.pairing = {'hash': hashlib.sha256(code.encode()).hexdigest(), 'expires': time.time() + PAIR_SECONDS}
            expires = self.pairing['expires']
        return '-'.join(code[i:i + 4] for i in range(0, 16, 4)), expires

    def cancel(self):
        with self.lock:
            self.pairing = None

    def limited(self, client):
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(client, []) if now - t < FAILURE_WINDOW]
            self.failures[client] = recent
            return len(recent) >= FAILURES

    def failed(self, client):
        with self.lock:
            self.failures.setdefault(client, []).append(time.time())

    def finish(self, client, code, name):
        """A new device cookie value for the right, unexpired code (which is then used up), else None."""
        now = time.time()
        digest = hashlib.sha256(normal_code(code).encode()).hexdigest()
        with self.lock:
            p = self.pairing
            if not (p and now < p['expires'] and hmac.compare_digest(digest, p['hash'])):
                self.failures.setdefault(client, []).append(now)
                return None
            self.pairing = None
            device, token = secrets.token_hex(8), secrets.token_urlsafe(32)
            devices = self.devices()
            devices[device] = {'name': name, 'hash': hashlib.sha256(token.encode()).hexdigest(),
                               'paired_at': int(now), 'last_used': int(now)}
            self._write('devices.json', {'devices': devices})
            return f'{device}.{token}'

    def check(self, value):
        """The device id for a valid, recently used cookie value, else None."""
        device, _, token = (value or '').partition('.')
        row = self.devices().get(device)
        if not (row and token and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row.get('hash', ''))):
            return None
        now = time.time()
        if now - (row.get('last_used') or 0) > DEVICE_IDLE:
            return None
        if now - (row.get('last_used') or 0) > 3600:  # sliding expiry, written at most hourly
            with self.lock:
                devices = self.devices()
                if device in devices:
                    devices[device]['last_used'] = int(now)
                    self._write('devices.json', {'devices': devices})
        return device

    def remove(self, device):
        with self.lock:
            devices = self.devices()
            removed = devices.pop(device, None)
            self._write('devices.json', {'devices': devices})
            return removed is not None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, public_host, root, caller=None, bind='127.0.0.1', restart=None, remote=None):
        self.root = root
        self.caller = caller or (lambda method, params: rpc(root, method, params))
        self.csrf = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(16)
        self.remote = remote or Remote(root)
        self.restart = restart or (lambda: threading.Timer(0.5, os._exit, (0,)).start())  # launchd starts us again
        super().__init__((bind, port), Handler)
        actual = self.server_address[1]
        self.port = actual
        self.origins = {f'http://127.0.0.1:{actual}', f'http://localhost:{actual}'}
        if public_host:
            if any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in public_host):
                self.server_close()
                raise ValueError('INVALID_PUBLIC_HOST')
            self.origins.add('https://' + public_host)
        self.hosts = {urlsplit(o).netloc for o in self.origins}
        self.public_host = public_host

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass  # Do not log request bodies, cookies or arbitrary exception text.

    def listen_addresses(self, settings=None):
        """Extra addresses to listen on for remote control (127.0.0.1 is always on)."""
        s = settings or self.remote.settings()
        if not s['enabled']:
            return []
        lan, tailnet = interface_addresses()
        return (lan if s['lan'] else []) + (tailnet if s['tailscale'] else [])

    def remote_hosts(self):
        """Hosts a paired device may use: the picked addresses with this port, and the public address(es)."""
        s = self.remote.settings()
        if not s['enabled']:
            return set()
        hosts = {f'{ip}:{self.port}' for ip in self.listen_addresses(s)}
        for public in (s['public'], self.public_host):
            if public:
                hosts.add(public)
        return hosts

    def pair_urls(self):
        """Addresses a phone could open, best first."""
        s = self.remote.settings()
        if not s['enabled']:
            return []
        urls = [f'https://{h}' for h in (s['public'], self.public_host) if h]
        lan, tailnet = interface_addresses()
        urls += [f'http://{ip}:{self.port}' for ip in (tailnet if s['tailscale'] else [])]
        urls += [f'http://{ip}:{self.port}' for ip in (lan if s['lan'] else [])]
        return list(dict.fromkeys(urls))


class Handler(BaseHTTPRequestHandler):
    server_version = 'AGY'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def respond(self, code, data, content='application/json; charset=utf-8', cookie=None):
        body = json.dumps(wire(data), ensure_ascii=False).encode() if not isinstance(data, bytes) else data
        self.send_response(code)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    @property
    def client(self):
        return self.client_address[0]

    def from_this_mac(self):
        try:
            return ipaddress.ip_address(self.client.split('%')[0]).is_loopback
        except ValueError:
            return False

    @property
    def local(self):
        """Made on this Mac to 127.0.0.1 / localhost (or the legacy --public-host while remote control is off).
        A proxy (ngrok, Caddy, tailscale serve) connects from this Mac too but with its own Host, so it is remote; a
        Wi-Fi client never passes the loopback test, whatever Host it sends."""
        return self.from_this_mac() and self.headers.get('Host') in self.server.hosts and not (
            self.headers.get('Host') == self.server.public_host and self.server.remote.settings()['enabled'])

    @property
    def scheme(self):
        forwarded = self.headers.get('X-Forwarded-Proto') if self.from_this_mac() else None
        if forwarded == 'https' or self.headers.get('Host') in (self.server.remote.settings()['public'], self.server.public_host):
            return 'https'  # public addresses are served by a TLS-terminating tunnel
        return 'http'

    def allowed(self):
        host = (self.headers.get('Host') or '').lower()
        if self.local:
            origins = self.server.origins
        elif host in self.server.remote_hosts():
            origins = {f'{self.scheme}://{host}'}
        else:
            self.respond(403, {'error': 'HOST_NOT_ALLOWED'})
            return False
        origin = self.headers.get('Origin')
        if (origin is not None and origin not in origins) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.respond(403, {'error': 'ORIGIN_NOT_ALLOWED'})
            return False
        return True

    def device(self):
        """The paired device behind this request, or None (a presented but invalid cookie counts as a failure)."""
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie') or '')
        except Exception:
            return None
        morsel = jar.get(COOKIE)
        if not morsel:
            return None
        found = self.server.remote.check(morsel.value)
        if not found:
            self.server.remote.failed(self.client)
        return found

    def gate(self, api):
        """True when a remote request may go on; otherwise answers it (pairing page / 401 / 429)."""
        if self.local:
            return True
        if self.server.remote.limited(self.client):
            self.respond(429, {'error': 'TOO_MANY_ATTEMPTS'})
            return False
        if self.device():
            return True
        if api:
            self.respond(401, {'error': 'PAIRING_REQUIRED'})
        else:
            self.redirect('/agy/pair')
        return False

    def do_GET(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path in ('/', '/agy'):
            self.redirect('/agy/')
            return
        public = {'/agy/pair': ('pair.html', 'text/html'), '/agy/pair.js': ('pair.js', 'text/javascript'),
                  '/agy/icon.svg': ('icon.svg', 'image/svg+xml'), '/agy/style.css': ('style.css', 'text/css'),
                  '/agy/backdrop.svg': ('backdrop.svg', 'image/svg+xml'),
                  '/agy/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
                  '/agy/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png')}
        if path not in public and not self.gate(api=path != '/agy/'):
            return
        if path == '/agy/api/status':
            try:
                status = self.server.caller('status', {})
            except Exception:
                self.respond(503, {'error': 'SERVICE_UNAVAILABLE'})
                return
            if not self.local:
                status = without_email(status)
            remote = {'local': self.local, 'full': self.server.remote.settings()['full']}
            self.respond(200, {'status': status, 'csrf': self.server.csrf, 'remote': remote})
            return
        assets = {**public,
                  '/agy/': ('index.html', 'text/html'),
                  '/agy/lens.js': ('lens.js', 'text/javascript'),
                  '/agy/qrcode.js': ('qrcode.js', 'text/javascript'),
                  '/agy/app.js': ('app.js', 'text/javascript')}
        if path not in assets:
            self.respond(404, {'error': 'NOT_FOUND'})
            return
        name, kind = assets[path]
        self.respond(200, (ASSETS / name).read_bytes(), kind if kind == 'image/png' else kind + '; charset=utf-8')

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise LookupError('JSON_REQUIRED')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 16384 or self.headers.get('Transfer-Encoding'):
            raise ValueError()
        return json.loads(self.rfile.read(length))

    def do_POST(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path == '/agy/api/pair':
            self.pair()
            return
        if path != '/agy/api/action':
            self.respond(404, {'error': 'NOT_FOUND'})
            return
        if not self.gate(api=True):
            return
        if not hmac.compare_digest(self.headers.get('X-AGY-CSRF', ''), self.server.csrf):
            self.respond(403, {'error': 'CSRF_REQUIRED'})
            return
        try:
            body = self.body()
            method, params = body['method'], body.get('params', {})
            if method not in METHODS | LOCAL_METHODS or not isinstance(params, dict):
                raise ValueError()
            if not self.local and (method in LOCAL_METHODS or
                                   (method not in REMOTE_METHODS and not self.server.remote.settings()['full'])):
                raise PermissionError()
            if method == 'config.set':
                revision = params.get('revision')
                if not isinstance(revision, str) or not revision.isdecimal():
                    raise ValueError()
                params['revision'] = int(revision)
        except LookupError:
            self.respond(415, {'error': 'JSON_REQUIRED'})
            return
        except PermissionError:
            self.respond(403, {'error': 'NOT_ALLOWED_REMOTELY'})
            return
        except (ValueError, KeyError, TypeError):
            self.respond(400, {'error': 'INVALID_REQUEST'})
            return
        if method in LOCAL_METHODS:
            self.local_action(method, params)
            return
        try:
            result = self.server.caller(method, params)
            self.respond(200, {'result': result if self.local else without_email(result)})
        except QuotaError as e:
            code = str(e)
            self.respond(409, {'error': code if code.replace('_', '').isalnum() else 'ACTION_FAILED'})
        except Exception:
            self.respond(503, {'error': 'SERVICE_UNAVAILABLE'})

    def remote_info(self):
        lan, tailnet = interface_addresses()
        return {**self.server.remote.settings(), 'devices': self.server.remote.list(), 'urls': self.server.pair_urls(),
                'lan_addresses': lan, 'tailscale_addresses': tailnet, 'ngrok': ngrok_hosts(), 'port': self.server.port}

    def local_action(self, method, params):
        remote = self.server.remote
        if method == 'remote.info':
            self.respond(200, {'result': self.remote_info()})
        elif method == 'remote.set':
            values = {k: v for k, v in params.items() if k in Remote.DEFAULTS}
            if not values or len(values) != len(params) or any(
                    type(v) is not type(Remote.DEFAULTS[k]) for k, v in values.items()):
                self.respond(400, {'error': 'INVALID_REQUEST'})
                return
            if 'public' in values:
                values['public'] = values['public'].strip().lower().removeprefix('https://').removeprefix('http://').rstrip('/')
                if values['public'] and not HOST.match(values['public']):
                    self.respond(400, {'error': 'INVALID_PUBLIC_ADDRESS'})
                    return
            before = self.server.listen_addresses()
            remote.set(values)
            restarting = self.server.listen_addresses() != before
            self.respond(200, {'result': {**self.remote_info(), 'restarting': restarting}})
            if restarting:
                self.server.restart()  # listen on the new addresses
        elif method == 'pair.start':
            if not remote.settings()['enabled']:
                self.respond(409, {'error': 'REMOTE_OFF'})
                return
            code, expires = remote.start()
            self.respond(200, {'result': {'code': code, 'expires': expires, 'urls': self.server.pair_urls()}})
        elif method == 'pair.cancel':
            remote.cancel()
            self.respond(200, {'result': {}})
        elif method == 'devices.remove':
            if not isinstance(params.get('id'), str) or not remote.remove(params['id']):
                self.respond(404, {'error': 'UNKNOWN_DEVICE'})
                return
            self.respond(200, {'result': self.remote_info()})

    def pair(self):
        remote = self.server.remote
        if self.local or not remote.settings()['enabled']:
            self.respond(409, {'error': 'REMOTE_OFF' if not remote.settings()['enabled'] else 'ALREADY_LOCAL'})
            return
        if remote.limited(self.client):
            self.respond(429, {'error': 'TOO_MANY_ATTEMPTS'})
            return
        try:
            code = self.body().get('code')
            if not isinstance(code, str) or len(code) > 64:
                raise ValueError()
        except LookupError:
            self.respond(415, {'error': 'JSON_REQUIRED'})
            return
        except (ValueError, KeyError, TypeError, AttributeError):
            self.respond(400, {'error': 'INVALID_REQUEST'})
            return
        value = remote.finish(self.client, code, device_name(self.headers.get('User-Agent')))
        if not value:
            self.respond(403, {'error': 'PAIRING_CODE_INVALID'})
            return
        cookie = f'{COOKIE}={value}; Path=/agy; Max-Age={DEVICE_IDLE}; HttpOnly; SameSite=Strict'
        if self.scheme == 'https':
            cookie += '; Secure'
        self.respond(200, {'result': {'paired': True}}, cookie=cookie)


def serve(port, public_host, home):
    """127.0.0.1 always; while remote control is on, also each picked Wi-Fi / Tailscale address (same handler)."""
    main = Server(port, public_host, home)
    for address in main.listen_addresses():
        try:
            extra = Server(port, public_host, home, caller=main.caller, bind=address, remote=main.remote)
        except OSError:
            continue  # the address went away; the remaining ones still work
        extra.csrf = main.csrf
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    main.serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=3082)
    parser.add_argument('--public-host', default='')
    parser.add_argument('--home', type=Path, default=Path.home() / '.agy-rotator')
    args = parser.parse_args()
    serve(args.port, args.public_host, args.home)
