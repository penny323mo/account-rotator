import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import isolation  # noqa: F401
from gemini_app import GeminiApp, read_config, with_active
from quota import QuotaError

# The real file's layout (2026-10-15): field 4 = in use, field 6 = {1: folder, 2: 2} per signed-in account
REAL = (b'\x22\x05user1' + b''.join(b'\x32\x09\x0a\x05' + u + b'\x10\x02'
                                    for u in (b'user1', b'user3', b'user4', b'user5', b'user6', b'user2')))


class FakeIO:
    def __init__(self):
        self.raw, self.up, self.log = REAL, True, []
        self.stuck = False

    def read(self):
        return self.raw

    def write(self, raw):
        self.log.append(('write', read_config(raw)[0], self.up))
        self.raw = raw

    def labels(self):
        return {'user2': 'GEMINI_A', 'user3': 'GEMINI_F'}

    def running(self):
        return self.up

    def quit(self):
        self.log.append('quit')
        self.up = self.stuck

    def open(self):
        self.log.append('open')
        self.up = True

    def sleep(self, seconds):
        pass


class GeminiAppTests(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.app = GeminiApp(self.io)

    def test_config_is_read_and_rewritten_byte_for_byte(self):
        self.assertEqual(read_config(REAL), ('user1', ['user1', 'user3', 'user4', 'user5', 'user6', 'user2']))
        self.assertEqual(with_active(with_active(REAL, 'user2'), 'user1'), REAL)
        self.assertEqual(with_active(REAL, 'user2')[7:], REAL[7:])

    def test_state_names_the_accounts(self):
        st = self.app.state()
        self.assertEqual(st['active'], 'user1')
        self.assertEqual([(a['id'], a['label']) for a in st['accounts']][:2], [('user1', None), ('user3', 'GEMINI_F')])

    def test_switch_closes_the_app_first_and_opens_it_again(self):
        st = self.app.use('user2')
        self.assertEqual(self.io.log, ['quit', ('write', 'user2', False), 'open'])
        self.assertEqual(st['active'], 'user2')

    def test_a_closed_app_is_switched_and_left_closed(self):
        self.io.up = False
        self.app.use('user3')
        self.assertEqual(self.io.log, [('write', 'user3', False)])

    def test_an_app_that_does_not_close_is_left_alone(self):
        self.io.stuck = True
        with self.assertRaises(QuotaError) as e:
            self.app.use('user2')
        self.assertEqual(str(e.exception), 'GEMINI_APP_STILL_OPEN')
        self.assertEqual(self.io.raw, REAL)

    def test_unknown_account_and_the_one_in_use(self):
        with self.assertRaises(QuotaError):
            self.app.use('user9')
        self.app.use('user1')
        self.assertEqual(self.io.log, [])


if __name__ == '__main__':
    unittest.main()
