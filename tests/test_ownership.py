"""Owned-process termination + rotation lock.  Fake processes only: harmless `sleep` children that THIS test
spawns (and always cleans up).  No real agy, no Keychain, all paths in tmp dirs."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'daemon'))
import agy_ownership as own
import broker as broker_mod
from quota import QuotaError

REAL_UNOWNED_AGY = broker_mod.unowned_agy


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        env = patch.dict(os.environ, {'AGY_ROTATOR_HOME': str(t), 'AGY_OWNED_DIR': str(t / 'owned'),
                                      'AGY_ROTATION_LOCK': str(t / 'rotation.lock')})
        env.start()
        self.addCleanup(env.stop)
        self.procs = []
        self.addCleanup(self.reap)
        self.t = t

    def spawn(self, exe='/bin/sleep', own_group=True):
        p = subprocess.Popen([exe, '60'], start_new_session=own_group,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def reap(self):  # only processes this test spawned
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL) if os.getpgid(p.pid) == p.pid else p.kill()
                except OSError:
                    pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def alive(self, p):
        return p.poll() is None


class RegistryTests(Base):
    def test_proc_info_reads_real_facts(self):
        pgid, state, start = own.proc_info(os.getpid())
        self.assertEqual(pgid, os.getpgrp())
        self.assertEqual(state, 'R')
        self.assertRegex(start, r'^\d+\.\d{6}$')
        self.assertIsNone(own.proc_info(2 ** 22 + 12345))

    def test_zombie_is_not_live(self):
        p = self.spawn()
        rec = own.register(p.pid, 'test')
        os.kill(p.pid, signal.SIGKILL)
        time.sleep(0.3)  # killed but not yet waited on = zombie
        self.assertEqual(own.classify(rec), 'stale')

    def test_only_registered_groups_are_terminated(self):
        a, b, c = self.spawn(), self.spawn(), self.spawn()
        own.register(a.pid, 'test'); own.register(b.pid, 'test')
        res = own.terminate_owned(wait=5)
        a.wait(timeout=5); b.wait(timeout=5)
        self.assertTrue(self.alive(c), 'unregistered process must survive')
        self.assertEqual(sorted(res['terminated']), sorted([a.pid, b.pid]))
        self.assertEqual(res['survivors'], [])
        self.assertEqual(list((self.t / 'owned').glob('*.json')), [])  # entries cleaned after exit

    def test_unregistered_process_named_agy_survives(self):
        link = self.t / 'agy'
        os.symlink('/bin/sleep', link)
        fake_agy = self.spawn(exe=str(link))
        reg = self.spawn()
        own.register(reg.pid, 'test')
        names = subprocess.run(['pgrep', '-x', 'agy'], capture_output=True, text=True).stdout.split()
        self.assertIn(str(fake_agy.pid), names, 'premise: pgrep -x agy would have matched it')
        own.terminate_owned(wait=5)
        reg.wait(timeout=5)
        self.assertTrue(self.alive(fake_agy))

    def test_pid_reuse_is_rejected(self):
        victim = self.spawn()
        rec = own.register(victim.pid, 'test')
        path = self.t / 'owned' / ('%d.json' % victim.pid)
        forged = dict(rec, start='1000000000.000000')  # same pid, different incarnation
        path.write_text(json.dumps(forged))
        self.assertEqual(own.classify(forged), 'reused')
        res = own.terminate_owned(wait=1)
        self.assertTrue(self.alive(victim))
        self.assertEqual(res['terminated'], [])
        self.assertFalse(path.exists(), 'reused entry is dropped without signalling')

    def test_pgid_mismatch_is_not_live(self):
        p = self.spawn()
        rec = own.register(p.pid, 'test')
        self.assertEqual(own.classify(dict(rec, pgid=rec['pgid'] + 1)), 'reused')

    def test_stale_entries_are_cleaned(self):
        p = self.spawn()
        own.register(p.pid, 'test')
        p.kill(); p.wait(timeout=5)
        (self.t / 'owned' / 'garbage.json').write_text('{not json')
        (self.t / 'owned' / '999999.json').write_text(json.dumps(
            {'version': 1, 'pid': 999999, 'pgid': 999999, 'start': 'x', 'registered_at': 0, 'launcher': 't'}))
        found = own.entries()
        self.assertTrue(all(s != 'live' for _, _, s in found))
        self.assertEqual(list((self.t / 'owned').glob('*.json')), [])

    def test_register_refuses_non_leader(self):
        p = self.spawn(own_group=False)  # shares the test runner's group
        with self.assertRaises(ValueError):
            own.register(p.pid, 'test')
        self.assertTrue(self.alive(p))

    def test_own_process_group_is_never_signalled(self):
        p = self.spawn()
        own.register(p.pid, 'test')
        with patch.object(os, 'getpgrp', return_value=p.pid):
            res = own.terminate_owned(wait=0.3)
        self.assertTrue(self.alive(p))
        self.assertEqual(res['terminated'], [])

    def test_group_children_die_with_the_leader(self):
        sh = subprocess.Popen(['/bin/sh', '-c', 'sleep 60 & echo $!; wait'], stdout=subprocess.PIPE,
                              start_new_session=True)
        self.procs.append(sh)
        child = int(sh.stdout.readline())
        own.register(sh.pid, 'test')
        own.terminate_owned(wait=5)
        sh.wait(timeout=5)
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_cli_spawn_registers_leader(self):
        log = self.t / 'lane.log'
        out = subprocess.run([sys.executable, str(Path(own.__file__)), 'spawn', '--launcher', 'unit',
                              '--log', str(log), '--', '/bin/sleep', '60'],
                             capture_output=True, text=True, env=dict(os.environ))
        pid = int(out.stdout.strip())
        self.addCleanup(lambda: os.killpg(pid, signal.SIGKILL) if own.proc_info(pid) else None)
        self.assertEqual(own.proc_info(pid)[0], pid)
        rec = json.loads((self.t / 'owned' / ('%d.json' % pid)).read_text())
        self.assertEqual((rec['pid'], rec['pgid'], rec['launcher']), (pid, pid, 'unit'))
        self.assertEqual(own.classify(rec), 'live')


class LockTests(Base):
    def test_acquire_release_and_exclusion(self):
        a, b = own.RotationLock(), own.RotationLock()
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire(timeout=0.2))
        st = own.lock_state()
        self.assertTrue(st['held'] and st['pid'] == os.getpid() and not st['stale'])
        a.release()
        self.assertFalse(own.lock_state()['held'])
        self.assertTrue(b.acquire()); b.release()

    def test_lock_state_does_not_take_the_lock(self):
        a = own.RotationLock()
        self.assertTrue(a.acquire())
        own.lock_state(); own.lock_state()
        a.release()
        self.assertTrue(own.RotationLock().acquire())

    def test_startup_check_free_waits_and_times_out(self):
        self.assertEqual(own.wait_for_rotation(timeout=0.5), 'FREE')
        a = own.RotationLock()
        self.assertTrue(a.acquire())
        self.assertEqual(own.wait_for_rotation(timeout=0.4, poll=0.05), 'TIMEOUT')
        threading.Timer(0.3, a.release).start()
        self.assertEqual(own.wait_for_rotation(timeout=3, poll=0.05), 'WAITED')

    def test_stale_holder_is_ignored_by_readers(self):
        a = own.RotationLock()
        self.assertTrue(a.acquire())
        old = time.time() - 500
        os.pwrite(a.fd, json.dumps({'pid': os.getpid(), 'ts': old, 'owner': 'x'}).ljust(100).encode(), 0)
        st = own.lock_state(stale_seconds=120)
        self.assertTrue(st['held'] and st['stale'])
        self.assertEqual(own.wait_for_rotation(timeout=0.2, stale_seconds=120), 'FREE')
        a.release()

    def test_dead_holder_cannot_wedge_the_lock(self):
        code = ("import sys; sys.path.insert(0, %r); import agy_ownership as o, time; l = o.RotationLock(); "
                "assert l.acquire(); print('held', flush=True); time.sleep(60)" % str(Path(own.__file__).parent))
        p = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), 'held')
        self.assertTrue(own.lock_state()['held'])
        p.kill(); p.wait(timeout=5)
        self.assertFalse(own.lock_state()['held'])
        self.assertEqual(own.wait_for_rotation(timeout=0.2), 'FREE')


class BrokerBase(Base):
    def setUp(self):
        super().setUp()
        p = patch.object(broker_mod, 'unowned_agy', return_value=[])  # no real ps in these tests
        p.start()
        self.addCleanup(p.stop)

    def make(self):
        profiles = types.SimpleNamespace(list=lambda: {'A': {}, 'B': {}}, read=lambda label: None,
                                         active=lambda: self.active[0])
        self.active = ['A']
        return broker_mod.Broker(self.t, profiles=profiles, helper='/nonexistent/agy-account')


class BrokerLockTests(BrokerBase):
    def test_lock_is_held_exactly_during_the_helper_call(self):
        b = self.make()
        seen = {}

        def fake_run(args, **kw):
            seen['held'] = own.lock_state()['held']
            self.active[0] = 'B'
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {'activation': 'OK', 'selected': 'B', 'app_reopened': False}).encode(), stderr=b'')
        with patch.object(broker_mod.subprocess, 'run', side_effect=fake_run):
            r = b.switch('B', close_all=True)
        self.assertTrue(seen['held'])
        self.assertEqual(r['selected'], 'B')
        self.assertFalse(own.lock_state()['held'])

    def test_lock_released_on_failure(self):
        b = self.make()
        with patch.object(broker_mod.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 1)):
            with self.assertRaises(QuotaError):
                b.switch('B', close_all=True)
        self.assertFalse(own.lock_state()['held'])

    def test_busy_lock_refuses_without_calling_helper(self):
        b = self.make()
        other = own.RotationLock()
        self.assertTrue(other.acquire())
        self.addCleanup(other.release)
        with patch.object(broker_mod.subprocess, 'run') as run:
            with self.assertRaises(QuotaError) as e:
                b.switch('B', close_all=True)  # waits its 2 s acquire window, then refuses
        self.assertEqual(str(e.exception), 'SWITCH_BUSY')
        run.assert_not_called()


class UnownedAgyTests(BrokerBase):
    ROWS = [('100', '100', 'agy -p do the card --model x'),          # owned dev lane
            ('200', '200', '/opt/homebrew/bin/agy --print research'),  # production print job, unowned
            ('300', '300', 'agy'),                                     # interactive, unowned
            ('400', '400', '/bin/sleep 5'), ('500', '500', 'python3 agy_ownership.py')]

    def test_classification_excludes_owned_groups(self):
        got = REAL_UNOWNED_AGY(self.ROWS, owned={100})
        self.assertEqual(sorted(got), [(200, True), (300, False)])

    def test_interactive_unowned_agy_refuses_before_lock_and_helper(self):
        b = self.make()
        with patch.object(broker_mod, 'unowned_agy', return_value=[(300, False)]), \
             patch.object(broker_mod, 'working', return_value=[]), \
             patch.object(broker_mod.subprocess, 'run') as run:
            with self.assertRaises(QuotaError) as e:
                b.switch('B', close_all=True)
        self.assertEqual(str(e.exception), 'AGY_UNREADABLE_NOT_SWITCHED')
        run.assert_not_called()
        self.assertFalse(own.lock_state()['held'])

    def test_unowned_print_job_gets_the_drain_window(self):
        b = self.make()
        calls = []

        def fake_run(args, **kw):
            calls.append(own.lock_state()['held'])
            self.active[0] = 'B'
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                {'activation': 'OK', 'selected': 'B'}).encode(), stderr=b'')
        with patch.object(broker_mod, 'unowned_agy', return_value=[(200, True)]), \
             patch.object(broker_mod.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(b.switch('B', close_all=True)['selected'], 'B')
        self.assertEqual(calls, [True])

    def test_refusal_retry_is_short_not_the_600s_cooldown(self):
        self.assertEqual(broker_mod.RETRY_AFTER['UNOWNED_AGY_NOT_SWITCHED'], 120)
        self.assertGreaterEqual(broker_mod.RETRY_AFTER['CLOSE_TIMEOUT_NOT_SWITCHED'], 120)
        self.assertGreaterEqual(broker_mod.RETRY_AFTER['AGY_RUNNING_NOT_SWITCHED'], 120)
        self.assertLess(broker_mod.RETRY_AFTER['CLOSE_TIMEOUT_NOT_SWITCHED'], 600)
        self.assertIn('UNOWNED_AGY_NOT_SWITCHED', broker_mod.NOT_SWITCHED)


class LockTimingInvariantTests(unittest.TestCase):
    """Real constants, no patching."""
    def test_hold_is_bounded_and_start_wait_exceeds_it(self):
        self.assertLessEqual(own.DRAIN_SECONDS + own.SWITCH_BUDGET_SECONDS, own.LOCK_MAX_HOLD_SECONDS)
        self.assertLessEqual(own.LOCK_MAX_HOLD_SECONDS, 40)
        self.assertGreaterEqual(own.DEFAULT_WAIT_SECONDS, 60)
        self.assertGreaterEqual(own.DEFAULT_WAIT_SECONDS - own.LOCK_MAX_HOLD_SECONDS, 10)
        self.assertGreater(own.DEFAULT_STALE_SECONDS, own.LOCK_MAX_HOLD_SECONDS)
        self.assertLess(own.DEFAULT_STALE_SECONDS, own.DEFAULT_WAIT_SECONDS)

    def test_helper_drain_matches_the_bound(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader('acct', str(Path(__file__).resolve().parents[1] / 'helpers' / 'agy-account'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec); loader.exec_module(mod)
        self.assertEqual(mod.DRAIN_SECONDS, own.DRAIN_SECONDS)
        import inspect
        self.assertEqual(inspect.signature(mod.close_all).parameters['wait'].default, own.DRAIN_SECONDS)


if __name__ == '__main__':
    unittest.main()
