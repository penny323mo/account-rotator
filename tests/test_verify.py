"""Google's "Verify your account to continue." (403 VALIDATION_REQUIRED) is shown with its link, not as stale data."""
import io
import sys
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))

import isolation  # noqa: F401
import quota
from quota import QuotaError
from service import Service

LINK = 'https://accounts.google.com/v3/signin/continue?plt=abc'


def forbidden(body):
    return urllib.error.HTTPError('https://x', 403, 'Forbidden', {}, io.BytesIO(json.dumps(body).encode()))


def validation(url=LINK):
    return {'error': {'code': 403, 'message': 'Verify your account to continue.', 'details': [
        {'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': 'VALIDATION_REQUIRED',
         'metadata': {'validation_url': url}}]}}


class RequestTests(unittest.TestCase):
    def call(self, error):
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(quota.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(QuotaError) as caught:
                quota.request(quota.BASE + 'retrieveUserQuotaSummary', {'project': 'p'}, 'token')
        return caught.exception

    def test_validation_required_carries_the_google_link(self):
        # GEMINI_F from 2026-11-16 20:00: every poll QUOTA_HTTP_403, the page only said 資料待更新
        e = self.call(forbidden(validation()))
        self.assertEqual((str(e), e.url), ('VERIFY_REQUIRED', LINK))

    def test_other_403_and_foreign_links_stay_plain(self):
        self.assertEqual(str(self.call(forbidden({'error': {'code': 403, 'details': []}}))), 'QUOTA_HTTP_403')
        self.assertEqual(str(self.call(forbidden(validation('https://evil.example/x')))), 'QUOTA_HTTP_403')


class RowTests(unittest.TestCase):
    def test_row_offers_the_link_until_the_account_answers_again(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        profiles = Mock()
        profiles.list.return_value = {'A': {}, 'F': {}}
        profiles.active.return_value = 'A'
        client = Mock()
        error = QuotaError('VERIFY_REQUIRED')
        error.url = LINK
        ok = lambda label: {'label': label, 'status': 'OK', 'updated_at': quota.stamp(), 'groups': []}
        client.fetch.side_effect = lambda label: ok(label) if label == 'A' else (_ for _ in ()).throw(error)
        s = Service(temp.name, profiles, client, Mock(), Mock(return_value=[]))
        s._refresh_gemini()
        self.assertEqual((s.rows['F']['error'], s.rows['F']['verify_url']), ('VERIFY_REQUIRED', LINK))
        client.fetch.side_effect = ok
        s._refresh_gemini()
        self.assertNotIn('verify_url', s.rows['F'])


if __name__ == '__main__':
    unittest.main()
