"""Tests never reach the real Codex / agy helpers.

A Service built without explicit codex / watch_io / adder used to get the real ones: every refresh then queried
the real Codex accounts and warm-up could send a real prompt. Importing this module swaps the defaults for inert
fakes that answer "nothing there" and refuse anything with a side effect."""
from unittest.mock import MagicMock
import service
from quota import QuotaError


def _refuse(*args, **kwargs):
    raise QuotaError('TEST_ISOLATED')


class InertCodex:
    def quota(self):
        return {'profiles': {}, 'active': None}
    use = continue_thread = warm = open_app = staticmethod(_refuse)

    def app_running(self):
        return False


def inert_watch():
    watch = MagicMock()
    watch.codex_working.return_value = []
    watch.claude_working.return_value = []
    watch.interrupted_threads.return_value = []
    return watch


def inert_adder():
    adder = MagicMock()
    adder.start.side_effect = adder.finish.side_effect = adder.status.side_effect = _refuse
    adder.cancel.side_effect = adder.remove.side_effect = _refuse
    return adder


service.CodexAccounts = InertCodex


class InertClaude:
    def quota(self):
        return {'profiles': {}, 'active': None}
    use = warm = staticmethod(_refuse)


service.ClaudeAccounts = InertClaude
service.WatchIO = inert_watch
service.AccountAdder = inert_adder


import agy_park  # noqa: E402


class InertParkIO:
    """No real agy is terminated from a test."""
    def argv(self, pid):
        return None

    cwd = argv

    def conversation(self, pid):
        return False, None

    def conversation_saved(self, conversation):
        return False

    def terminate(self, pid):
        raise agy_park.ParkError('TEST_ISOLATED')

    def alive(self, pid):
        return False

    def sleep(self, seconds):
        pass


agy_park.SystemIO = InertParkIO
