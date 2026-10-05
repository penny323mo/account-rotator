import http.client
import importlib.util
import json
import threading
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
