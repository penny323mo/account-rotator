import http.client
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('agy_web', Path(__file__).resolve().parents[1] / 'web/server.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        def call(method, params):
            self.calls.append((method, params))
            return {'revision': 1790336540468068123, 'active': 'A'}
        self.server = web.Server(0, 'example.test', '/unused', call)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method='GET', path='/agy/api/status', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        result = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return result

    def post(self, value, **headers):
        return self.request('POST', '/agy/api/action', json.dumps(value),
                            {'Content-Type': 'application/json', 'X-AGY-CSRF': self.server.csrf, **headers})

    def test_status_preserves_revision_and_issues_csrf(self):
        code, headers, body = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['status']['revision'], '1790336540468068123')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_cross_origin_and_dns_rebinding_cannot_read_or_write(self):
        for headers in ({'Origin':'https://evil.test'}, {'Host':'evil.test'}, {'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request(headers=headers)[0], 403)
            self.assertEqual(self.post({'method':'enabled.set','params':{'enabled':True}}, **headers)[0],403)
        self.assertEqual(self.calls, [])

    def test_csrf_and_json_required(self):
        self.assertEqual(self.post({'method':'refresh'}, **{'X-AGY-CSRF':''})[0],403)
        self.assertEqual(self.post({'method':'refresh'}, **{'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.calls, [])

    def test_no_get_actions_or_arbitrary_rpc(self):
        self.assertEqual(self.request(path='/agy/api/action?method=use')[0],404)
        self.assertEqual(self.post({'method':'daemon'})[0],400)
        self.assertEqual(self.request(path='/agy/../../daemon/quota.py')[0],404)
        self.assertEqual(self.calls, [])

    def test_revision_exact_round_trip_and_no_number_coercion(self):
        self.assertEqual(self.post({'method':'config.set','params':{'revision':'1790336540468068123'}})[0],200)
        self.assertEqual(self.calls[-1][1]['revision'],1790336540468068123)
        self.assertEqual(self.post({'method':'config.set','params':{'revision':1790336540468068123}})[0],400)

    def test_public_host_and_same_origin_work(self):
        self.assertEqual(self.post({'method':'refresh'},Host='example.test',Origin='https://example.test')[0],200)
        self.assertEqual(self.calls,[('refresh',{})])

    def test_invalid_bodies_are_rejected(self):
        for value in ([],None,{'method':[]},{'method':'use','params':[]}):
            self.assertEqual(self.post(value)[0],400)
        self.assertEqual(self.request('POST','/agy/api/action','x'*16385,{'Content-Type':'application/json','X-AGY-CSRF':self.server.csrf})[0],400)
        self.assertEqual(self.calls,[])

    def test_assets(self):
        for path in ('/agy/','/agy/app.js','/agy/style.css'):
            code, _, body=self.request(path=path)
            self.assertEqual(code,200)
            self.assertGreater(len(body),100)


class RemoteTests(unittest.TestCase):
    """Remote control on, with a public address (as a tunnel would use): pairing, limits, removal."""
    PUBLIC = 'phone.test'

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        (Path(self.home.name) / 'remote.json').write_text(json.dumps({'enabled': True, 'public': self.PUBLIC}))
        self.calls = []
        def call(method, params):
            self.calls.append((method, params))
            return {'revision': 1, 'active': 'A', 'codex': {'profiles': {'CODEX_A': {'email': 'a@example.com', '5h': {}}}}}
        self.restarts = []
        self.server = web.Server(0, '', self.home.name, call, restart=lambda: self.restarts.append(1))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method='GET', path='/agy/api/status', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        result = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return result

    def local(self, method, params=None):
        code, _, body = self.request('POST', '/agy/api/action', json.dumps({'method': method, 'params': params or {}}),
                                     {'Content-Type': 'application/json', 'X-AGY-CSRF': self.server.csrf})
        return code, json.loads(body)

    def pair(self, code, host=None):
        return self.request('POST', '/agy/api/pair', json.dumps({'code': code}),
                            {'Content-Type': 'application/json', 'Host': host or self.PUBLIC,
                             'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)'})

    def phone(self, cookie, method='refresh', params=None, host=None):
        return self.request('POST', '/agy/api/action', json.dumps({'method': method, 'params': params or {}}),
                            {'Content-Type': 'application/json', 'X-AGY-CSRF': self.server.csrf,
                             'Host': host or self.PUBLIC, 'Cookie': cookie})[0]

    def paired(self):
        code = self.local('pair.start')[1]['result']['code']
        status, headers, _ = self.pair(code)
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0]

    def test_unpaired_device_gets_the_pairing_page_not_the_console(self):
        status, headers, _ = self.request(path='/agy/', headers={'Host': self.PUBLIC})
        self.assertEqual((status, headers['Location']), (302, '/agy/pair'))
        self.assertEqual(self.request(path='/agy/pair', headers={'Host': self.PUBLIC})[0], 200)
        self.assertEqual(self.request(headers={'Host': self.PUBLIC})[0], 401)
        self.assertEqual(self.calls, [])

    def test_host_outside_the_list_is_refused(self):
        self.assertEqual(self.request(path='/agy/pair', headers={'Host': 'evil.test'})[0], 403)
        self.assertEqual(self.pair('x', host='evil.test')[0], 403)

    def test_code_is_single_use_and_expires(self):
        code = self.local('pair.start')[1]['result']['code']
        self.assertEqual(self.pair(code)[0], 200)
        self.assertEqual(self.pair(code)[0], 403)
        code = self.local('pair.start')[1]['result']['code']
        self.server.remote.pairing['expires'] = time.time() - 1
        self.assertEqual(self.pair(code)[0], 403)

    def test_typed_code_works_like_the_qr_code(self):
        code = self.local('pair.start')[1]['result']['code']
        self.assertRegex(code, r'^[A-Z0-9]{4}(-[A-Z0-9]{4}){3}$')
        self.assertEqual(self.pair(' ' + code.replace('-', ' ').lower() + ' ')[0], 200)

    def test_paired_phone_switches_but_cannot_add_accounts(self):
        cookie = self.paired()
        self.assertEqual(self.phone(cookie, 'use', {'label': 'B'}), 200)
        self.assertEqual(self.calls[-1], ('use', {'label': 'B'}))
        for method in ('accounts.add.start', 'accounts.remove', 'provider.reset', 'config.set', 'warmup.set',
                       'codex.autocontinue.set', 'pair.start', 'devices.remove', 'remote.set', 'remote.info'):
            self.assertEqual(self.phone(cookie, method), 403, method)

    def test_full_control_opens_the_rest_but_never_pairing(self):
        cookie = self.paired()
        self.assertEqual(self.local('remote.set', {'full': True})[0], 200)
        self.assertEqual(self.phone(cookie, 'config.defaults'), 200)
        self.assertEqual(self.phone(cookie, 'pair.start'), 403)

    def test_remote_status_has_no_email(self):
        cookie = self.paired()
        _, _, body = self.request(headers={'Host': self.PUBLIC, 'Cookie': cookie})
        data = json.loads(body)
        self.assertNotIn('email', json.dumps(data['status']))
        self.assertEqual(data['remote'], {'local': False, 'full': False})
        self.assertIn('a@example.com', self.request()[2].decode())  # on this Mac it is still shown

    def test_removed_device_must_pair_again(self):
        cookie = self.paired()
        device = cookie.split('=', 1)[1].split('.')[0]
        self.assertEqual(self.local('devices.remove', {'id': device})[0], 200)
        self.assertEqual(self.phone(cookie), 401)
        status, headers, _ = self.request(path='/agy/', headers={'Host': self.PUBLIC, 'Cookie': cookie})
        self.assertEqual((status, headers['Location']), (302, '/agy/pair'))

    def test_device_unused_for_90_days_expires(self):
        cookie = self.paired()
        devices = json.loads((Path(self.home.name) / 'devices.json').read_text())
        for row in devices['devices'].values():
            row['last_used'] = int(time.time()) - web.DEVICE_IDLE - 10
        (Path(self.home.name) / 'devices.json').write_text(json.dumps(devices))
        self.assertEqual(self.phone(cookie), 401)

    def test_only_a_hash_is_stored_and_the_name_comes_from_the_browser(self):
        cookie = self.paired()
        stored = (Path(self.home.name) / 'devices.json').read_text()
        self.assertNotIn(cookie.split('.', 1)[1], stored)
        self.assertEqual([d['name'] for d in self.local('remote.info')[1]['result']['devices']], ['iPhone'])

    def test_cookie_flags(self):
        code = self.local('pair.start')[1]['result']['code']
        cookie = self.pair(code)[1]['Set-Cookie']
        for flag in ('agy_device=', 'HttpOnly', 'SameSite=Strict', 'Path=/agy', 'Secure'):  # public = HTTPS tunnel
            self.assertIn(flag, cookie)

    def test_wrong_codes_and_forged_cookies_are_rate_limited(self):
        for _ in range(web.FAILURES):
            self.assertEqual(self.pair('WRONG')[0], 403)
        code = self.local('pair.start')[1]['result']['code']
        self.assertEqual(self.pair(code)[0], 429)  # even the right code, for a minute
        self.server.remote.failures.clear()
        for _ in range(web.FAILURES):
            self.assertEqual(self.phone('agy_device=abc.forged'), 401)
        self.assertEqual(self.phone('agy_device=abc.forged'), 429)

    def test_turning_remote_off_refuses_other_hosts_and_pairing(self):
        cookie = self.paired()
        self.assertEqual(self.local('remote.set', {'enabled': False})[0], 200)
        self.assertEqual(self.phone(cookie), 403)
        self.assertEqual(self.local('pair.start')[0], 409)

    def test_settings_are_validated(self):
        self.assertEqual(self.local('remote.set', {'lan': 'yes'})[0], 400)
        self.assertEqual(self.local('remote.set', {'nope': True})[0], 400)
        self.assertEqual(self.local('remote.set', {'public': 'bad host!'})[0], 400)
        self.assertEqual(self.local('remote.set', {'public': 'https://Abc.ngrok-free.app/'})[1]['result']['public'],
                         'abc.ngrok-free.app')


class TunnelTests(RemoteTests):
    """Tunnels (ngrok, cloudflared, Caddy) connect from this Mac: they must never pass for this Mac."""
    def setUp(self):
        super().setUp()
        self.tunnel = web.Server(0, '', self.home.name, self.server.caller, remote=self.server.remote,
                                 always_remote=True)
        self.tunnel.csrf = self.server.csrf
        threading.Thread(target=self.tunnel.serve_forever, daemon=True).start()
        self.addCleanup(self.tunnel.server_close)
        self.addCleanup(self.tunnel.shutdown)

    def test_host_rewritten_to_localhost_with_a_proxy_header_is_remote(self):
        # Refused outright (127.0.0.1 is not a remote address); tunnels belong on the tunnel port.
        local = f'127.0.0.1:{self.port}'
        for header in ('X-Forwarded-For', 'Forwarded', 'Via', 'CF-Connecting-IP', 'X-Real-IP', 'X-Forwarded-Host'):
            self.assertEqual(self.request(headers={'Host': local, header: '203.0.113.9'})[0], 403, header)
            self.assertEqual(self.request(path='/agy/', headers={'Host': local, header: '203.0.113.9'})[0], 403, header)
            self.assertEqual(self.request('POST', '/agy/api/action', json.dumps({'method': 'refresh'}),
                                          {'Host': local, header: '203.0.113.9', 'Content-Type': 'application/json',
                                           'X-AGY-CSRF': self.server.csrf})[0], 403, header)
        self.assertEqual(self.calls, [])

    def test_tunnel_port_is_always_remote_even_with_a_rewritten_host(self):
        port = self.tunnel.server_address[1]
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        conn.request('GET', '/agy/api/status', headers={'Host': f'127.0.0.1:{port}'})
        self.assertEqual(conn.getresponse().status, 401)
        conn.close()
        code = self.local('pair.start')[1]['result']['code']
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        conn.request('POST', '/agy/api/pair', json.dumps({'code': code}),
                     {'Content-Type': 'application/json', 'Host': f'127.0.0.1:{port}', 'Origin': f'https://{self.PUBLIC}'})
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertIn('Secure', res.getheader('Set-Cookie'))
        conn.close()
        self.assertEqual(self.calls, [])

    def test_strangers_cannot_lock_out_a_paired_phone(self):
        cookie = self.paired()
        for _ in range(web.FAILURES + 3):
            self.request(
                'POST', '/agy/api/pair', json.dumps({'code': 'WRONG'}),
                {'Content-Type': 'application/json', 'Host': self.PUBLIC, 'X-Forwarded-For': '198.51.100.7'})
        status = self.request('POST', '/agy/api/pair', json.dumps({'code': 'WRONG'}),
                              {'Content-Type': 'application/json', 'Host': self.PUBLIC, 'X-Forwarded-For': '198.51.100.7'})[0]
        self.assertEqual(status, 429)                      # the stranger is limited ...
        self.assertEqual(self.phone(cookie), 200)          # ... the paired phone is not
        status = self.request('POST', '/agy/api/pair', json.dumps({'code': 'WRONG'}),
                              {'Content-Type': 'application/json', 'Host': self.PUBLIC, 'X-Forwarded-For': '192.0.2.4'})[0]
        self.assertEqual(status, 403)                      # another visitor is counted separately

    def test_a_used_cookie_is_sent_again(self):
        cookie = self.paired()
        _, headers, _ = self.request(headers={'Host': self.PUBLIC, 'Cookie': cookie})
        self.assertTrue(headers['Set-Cookie'].startswith(cookie))

    def test_sign_in_details_only_with_full_control(self):
        cookie = self.paired()
        self.server.caller = lambda m, p: {'revision': 1, 'enrolling': {'url': 'https://sign.in/x', 'code': 'ABCD'}}
        _, _, body = self.request(headers={'Host': self.PUBLIC, 'Cookie': cookie})
        self.assertNotIn('enrolling', json.loads(body)['status'])
        self.local('remote.set', {'full': True})
        _, _, body = self.request(headers={'Host': self.PUBLIC, 'Cookie': cookie})
        self.assertIn('enrolling', json.loads(body)['status'])

    def test_turning_remote_on_or_off_restarts_for_the_tunnel_port(self):
        self.assertTrue(self.local('remote.set', {'enabled': False})[1]['result']['restarting'])


class AddressTests(unittest.TestCase):
    IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.27 netmask 0xffffff00 broadcast 192.168.1.255
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.64.1 netmask 0xffffff00 broadcast 192.168.64.255
vmnet8: flags=8863<UP> mtu 1500
\tinet 172.16.5.1 netmask 0xffffff00
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.105.1.2 --> 100.105.1.2 netmask 0xffffffff
utun5: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff
"""

    def test_only_wifi_ethernet_and_tailscale_addresses(self):
        self.assertEqual(web.interface_addresses(self.IFCONFIG), (['192.168.1.27'], ['100.105.1.2']))


class ForwardedKeyTests(RemoteTests):
    def test_limit_counts_the_address_the_tunnel_appended(self):
        # A sender cannot dodge the limit by putting a new address first: the tunnel appends the real one last.
        for i in range(web.FAILURES):
            self.request('POST', '/agy/api/pair', json.dumps({'code': 'WRONG'}),
                         {'Content-Type': 'application/json', 'Host': self.PUBLIC,
                          'X-Forwarded-For': f'10.0.0.{i}, 198.51.100.7'})
        status = self.request('POST', '/agy/api/pair', json.dumps({'code': 'WRONG'}),
                              {'Content-Type': 'application/json', 'Host': self.PUBLIC,
                               'X-Forwarded-For': '10.9.9.9, 198.51.100.7'})[0]
        self.assertEqual(status, 429)


class SpoofedHeaderKeyTests(RemoteTests):
    def test_x_real_ip_cannot_replace_the_appended_address(self):
        for i in range(web.FAILURES):
            self.request('POST', '/agy/api/pair', json.dumps({'code': 'WRONG'}),
                         {'Content-Type': 'application/json', 'Host': self.PUBLIC, 'X-Real-IP': f'10.1.1.{i}',
                          'X-Forwarded-For': '198.51.100.7'})
        status = self.request('POST', '/agy/api/pair', json.dumps({'code': 'WRONG'}),
                              {'Content-Type': 'application/json', 'Host': self.PUBLIC, 'X-Real-IP': '10.2.2.2',
                               'X-Forwarded-For': '198.51.100.7'})[0]
        self.assertEqual(status, 429)


class RemoteOffProxyTests(WebTests):
    def test_with_remote_off_a_proxy_header_changes_nothing(self):
        # Off means exactly the old behaviour (an existing Caddy in front keeps working).
        self.assertEqual(self.request(headers={'X-Forwarded-For': '203.0.113.9'})[0], 200)


class RemoteOffTests(unittest.TestCase):
    def test_off_by_default_listens_on_loopback_only(self):
        with tempfile.TemporaryDirectory() as home:
            server = web.Server(0, '', home, lambda m, p: {})
            try:
                self.assertEqual(server.server_address[0], '127.0.0.1')
                self.assertEqual((server.listen_addresses(), server.remote_hosts(), server.pair_urls()), ([], set(), []))
            finally:
                server.server_close()


class LanTests(unittest.TestCase):
    """Listening on the Wi-Fi: a client on the network is remote even when it claims Host 127.0.0.1."""
    def setUp(self):
        self.ips = web.lan_addresses()
        if not self.ips:
            self.skipTest('no private network address on this machine')
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        (Path(self.home.name) / 'remote.json').write_text(json.dumps({'enabled': True, 'lan': True}))
        self.calls = []
        self.server = web.Server(0, '', self.home.name, lambda m, p: self.calls.append(m) or {'revision': 1},
                                 bind=self.ips[0], restart=lambda: None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def get(self, host):
        conn = http.client.HTTPConnection(self.ips[0], self.port, timeout=3)
        conn.request('GET', '/agy/api/status', headers={'Host': host})
        status = conn.getresponse().status
        conn.close()
        return status

    def test_spoofed_local_host_from_the_network_is_refused(self):
        self.assertEqual(self.get(f'127.0.0.1:{self.port}'), 403)

    def test_the_wifi_address_needs_pairing(self):
        self.assertEqual(self.get(f'{self.ips[0]}:{self.port}'), 401)
        self.assertEqual(self.calls, [])
