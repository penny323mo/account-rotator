#!/usr/bin/env python3
"""Loopback-only web bridge. It has no login of its own: never expose it beyond this Mac.

Do not point a tunnel at this port. Remote access (with its own login) is a later
feature. No credentials reach this process.
"""
import argparse
import hmac
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
from service import rpc
from quota import QuotaError

ASSETS = Path(__file__).parent
METHODS = {'refresh', 'enabled.set', 'config.set', 'config.defaults', 'use',
           'cancel', 'pause', 'resume', 'recovery.ack', 'codex.use', 'codex.auto.set', 'codex.autocontinue.set', 'warmup.set', 'accounts.add.start', 'accounts.add.finish', 'accounts.add.cancel', 'accounts.remove', 'provider.reset', 'claude.use'}


def wire(value):
    # Revisions are nanosecond integers: JavaScript numbers cannot round-trip them.
    if isinstance(value, dict):
        return {k: str(v) if k == 'revision' else wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [wire(v) for v in value]
    return value


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, public_host, root, caller=None):
        self.root = root
        self.caller = caller or (lambda method, params: rpc(root, method, params))
        self.csrf = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1', port), Handler)
        actual = self.server_address[1]
        self.origins = {f'http://127.0.0.1:{actual}', f'http://localhost:{actual}'}
        if public_host:
            if any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in public_host):
                self.server_close()
                raise ValueError('INVALID_PUBLIC_HOST')
            self.origins.add('https://' + public_host)
        self.hosts = {urlsplit(o).netloc for o in self.origins}

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


class Handler(BaseHTTPRequestHandler):
    server_version = 'AGY'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def respond(self, code, data, content='application/json; charset=utf-8'):
        body = json.dumps(wire(data), ensure_ascii=False).encode() if not isinstance(data, bytes) else data
        self.send_response(code)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        if self.headers.get('Host') not in self.server.hosts:
            self.respond(403, {'error': 'HOST_NOT_ALLOWED'})
            return False
        origin = self.headers.get('Origin')
        if ((origin is not None and origin not in self.server.origins) or
                self.headers.get('Sec-Fetch-Site') == 'cross-site'):
            self.respond(403, {'error': 'ORIGIN_NOT_ALLOWED'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = urlsplit(self.path).path
        if path in ('/', '/agy'):
            self.send_response(302)
            self.send_header('Location', '/agy/')
            self.end_headers()
            return
        if path == '/agy/api/status':
            try:
                self.respond(200, {'status': self.server.caller('status', {}), 'csrf': self.server.csrf})
            except Exception:
                self.respond(503, {'error': 'SERVICE_UNAVAILABLE'})
            return
        assets = {'/agy/backdrop.svg': ('backdrop.svg', 'image/svg+xml'),
                  '/agy/icon.svg': ('icon.svg', 'image/svg+xml'), '/agy/': ('index.html', 'text/html'),
                  '/agy/lens.js': ('lens.js', 'text/javascript'),
                  '/agy/app.js': ('app.js', 'text/javascript'),
                  '/agy/style.css': ('style.css', 'text/css')}
        if path not in assets:
            self.respond(404, {'error': 'NOT_FOUND'})
            return
        name, kind = assets[path]
        self.respond(200, (ASSETS / name).read_bytes(), kind + '; charset=utf-8')

    def do_POST(self):
        if not self.allowed():
            return
        if urlsplit(self.path).path != '/agy/api/action':
            self.respond(404, {'error': 'NOT_FOUND'})
            return
        if not hmac.compare_digest(self.headers.get('X-AGY-CSRF', ''), self.server.csrf):
            self.respond(403, {'error': 'CSRF_REQUIRED'})
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.respond(415, {'error': 'JSON_REQUIRED'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384 or self.headers.get('Transfer-Encoding'):
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            method, params = body['method'], body.get('params', {})
            if method not in METHODS or not isinstance(params, dict):
                raise ValueError()
            if method == 'config.set':
                revision = params.get('revision')
                if not isinstance(revision, str) or not revision.isdecimal():
                    raise ValueError()
                params['revision'] = int(revision)
        except (ValueError, KeyError, TypeError):
            self.respond(400, {'error': 'INVALID_REQUEST'})
            return
        try:
            result = self.server.caller(method, params)
            self.respond(200, {'result': result})
        except QuotaError as e:
            code = str(e)
            self.respond(409, {'error': code if code.replace('_', '').isalnum() else 'ACTION_FAILED'})
        except Exception:
            self.respond(503, {'error': 'SERVICE_UNAVAILABLE'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=3082)
    parser.add_argument('--public-host', default='')
    parser.add_argument('--home', type=Path, default=Path.home() / '.agy-rotator')
    args = parser.parse_args()
    Server(args.port, args.public_host, args.home).serve_forever()
