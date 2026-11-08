import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import idb

UDID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"


class Harness(unittest.TestCase):
    """Every process-touching call is replaced, so the tests exercise up()/down()'s decisions rather
    than the machine's."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-idb-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = idb.paths.STATE_DIR
        idb.paths.STATE_DIR = self.dir
        self.addCleanup(setattr, idb.paths, "STATE_DIR", saved)

        self.started_companions = []
        self.started_sidecars = []
        self.killed = []
        self.listening = set()
        self.live_pids = set()
        self.sidecar_answers = True

        def patch(name, value):
            original = getattr(idb, name)
            setattr(idb, name, value)
            self.addCleanup(setattr, idb, name, original)

        patch("ensure_venv", lambda *a, **k: "/nope/python3")
        patch("free_port", lambda: 5000 + len(self.started_companions))
        patch("_pid_alive", lambda pid: pid in self.live_pids)
        patch("_companion_listening", lambda port, timeout=30.0: port in self.listening)
        patch("alive", lambda udid: self.sidecar_answers and bool(self.started_sidecars))
        patch("_start_companion", self._start_companion)
        patch("_start_sidecar", self._start_sidecar)
        patch("send", self._send)
        patch("os", _OsProxy(self.killed))

    def _start_companion(self, udid, port, log_path):
        pid = 100 + len(self.started_companions)
        self.started_companions.append((udid, port, pid))
        self.live_pids.add(pid)
        self.listening.add(port)
        return pid

    def _start_sidecar(self, udid, port, log_path):
        pid = 200 + len(self.started_sidecars)
        self.started_sidecars.append((udid, port, pid))
        self.live_pids.add(pid)
        return pid

    def _send(self, ops, udid, timeout=None):
        return {"ok": True, "results": []}


class _OsProxy:
    """os, except kill() is recorded instead of signalling a real process."""

    def __init__(self, killed):
        self._killed = killed

    def __getattr__(self, name):
        return getattr(os, name)

    def kill(self, pid, signal_number):
        self._killed.append((pid, signal_number))


class UpTests(Harness):
    def test_a_live_sidecar_starts_nothing(self):
        idb.write_state(UDID, {"udid": UDID, "port": 5000, "companionPid": 100, "sidecarPid": 200})
        self.started_sidecars.append((UDID, 5000, 200))
        state = idb.up(UDID)
        self.assertEqual(state["sidecarPid"], 200)
        self.assertEqual(self.started_companions, [])

    def test_reuses_a_companion_that_is_still_listening(self):
        """The cached port and pid must survive into the reuse decision; clearing state first made
        this branch unreachable and leaked a companion on every restart."""
        idb.write_state(UDID, {"udid": UDID, "port": 4321, "companionPid": 100})
        self.live_pids.add(100)
        self.listening.add(4321)
        state = idb.up(UDID)
        self.assertEqual(self.started_companions, [])
        self.assertEqual(state["port"], 4321)
        self.assertEqual(state["companionPid"], 100)

    def test_starts_a_companion_when_the_cached_one_is_dead(self):
        idb.write_state(UDID, {"udid": UDID, "port": 4321, "companionPid": 999})
        idb.up(UDID)
        self.assertEqual(len(self.started_companions), 1)

    def test_records_the_companion_before_proving_it_works(self):
        """An unrecorded pid is an orphan `down` can never reap, so it is written to state the
        moment the process exists rather than once startup succeeds."""
        idb._companion_listening = lambda port, timeout=30.0: False
        with self.assertRaises(idb.IdbUnavailable):
            idb.up(UDID)
        self.assertEqual(idb.read_state(UDID)["companionPid"], self.started_companions[0][2])

    def test_records_the_sidecar_even_when_it_never_answers(self):
        self.sidecar_answers = False
        with self.assertRaises(idb.IdbUnavailable):
            idb.up(UDID, timeout=0.05)
        self.assertEqual(idb.read_state(UDID)["sidecarPid"], self.started_sidecars[0][2])

    def test_does_not_start_a_second_companion_per_restart(self):
        idb.up(UDID)
        self.started_sidecars.pop()
        self.sidecar_answers = False
        try:
            idb.up(UDID, timeout=0.05)
        except idb.IdbUnavailable:
            pass
        self.assertEqual(len(self.started_companions), 1)


class DownTests(Harness):
    def test_kills_both_recorded_processes(self):
        idb.write_state(UDID, {"udid": UDID, "port": 5000, "companionPid": 100, "sidecarPid": 200})
        self.live_pids.update({100, 200})
        idb.down(UDID)
        self.assertEqual(sorted(pid for pid, _ in self.killed), [100, 200])

    def test_forgets_the_pair_afterwards(self):
        idb.write_state(UDID, {"udid": UDID, "port": 5000, "companionPid": 100, "sidecarPid": 200})
        idb.down(UDID)
        self.assertEqual(idb.read_state(UDID), {})

    def test_survives_a_partial_record(self):
        idb.write_state(UDID, {"udid": UDID, "port": 5000, "companionPid": 100})
        self.live_pids.add(100)
        idb.down(UDID)
        self.assertEqual([pid for pid, _ in self.killed], [100])


class SocketPathTests(unittest.TestCase):
    def test_stays_inside_the_af_unix_limit(self):
        deep = "/Users/someone/" + "nested/" * 20 + "state"
        self.assertLessEqual(len(idb.socket_path(UDID, state=deep)), idb.SOCKET_PATH_LIMIT)

    def test_is_stable_for_one_udid_and_state_dir(self):
        deep = "/Users/someone/" + "nested/" * 20 + "state"
        self.assertEqual(idb.socket_path(UDID, state=deep), idb.socket_path(UDID, state=deep))

    def test_distinct_state_dirs_do_not_share_a_socket(self):
        a = "/Users/someone/" + "nested/" * 20 + "one"
        b = "/Users/someone/" + "nested/" * 20 + "two"
        self.assertNotEqual(idb.socket_path(UDID, state=a), idb.socket_path(UDID, state=b))

    def test_short_paths_stay_next_to_the_state_dir(self):
        self.assertTrue(idb.socket_path(UDID, state="/tmp/s").startswith("/tmp/s/"))


if __name__ == "__main__":
    unittest.main()
