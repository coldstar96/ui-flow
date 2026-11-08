import json
import os
import plistlib
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import session


DRIVER = "dev.uiflow.driver.tests.xctrunner"
SLYDER = "io.dtails.Slyder.SliderUITests.xctrunner"


def make_container(root, name, identifier, ready_at=None, heartbeat_at=None):
    path = os.path.join(root, name)
    os.makedirs(os.path.join(path, "tmp"), exist_ok=True)
    with open(os.path.join(path, ".com.apple.mobile_container_manager.metadata.plist"), "wb") as handle:
        plistlib.dump({"MCMMetadataIdentifier": identifier}, handle)
    if ready_at is not None:
        ready = os.path.join(path, "tmp", "ready")
        with open(ready, "w") as handle:
            handle.write(json.dumps({"pid": 1234, "at": ready_at}))
        os.utime(ready, (ready_at, ready_at))
    if heartbeat_at is not None:
        beat = os.path.join(path, "tmp", "heartbeat")
        with open(beat, "w") as handle:
            handle.write("1")
        os.utime(beat, (heartbeat_at, heartbeat_at))
    return os.path.join(path, "tmp")


class FindChannelTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="uiflow-containers-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_exact_match_ignores_coresident_slyder_runner(self):
        slyder = make_container(self.root, "AAAA", SLYDER, ready_at=time.time())
        mine = make_container(self.root, "BBBB", DRIVER, ready_at=time.time())
        found = session.find_channel(root=self.root)
        self.assertEqual(found, mine)
        self.assertNotEqual(found, slyder)

    def test_substring_only_match_is_not_accepted(self):
        make_container(self.root, "AAAA", SLYDER, ready_at=time.time())
        make_container(self.root, "CCCC", "dev.uiflow.driver.tests.xctrunner.old", ready_at=time.time())
        self.assertIsNone(session.find_channel(root=self.root))

    def test_two_candidates_resolve_to_the_newest_ready(self):
        now = time.time()
        make_container(self.root, "AAAA", DRIVER, ready_at=now - 500)
        newest = make_container(self.root, "BBBB", DRIVER, ready_at=now - 5)
        self.assertEqual(session.find_channel(root=self.root), newest)

    def test_ready_older_than_launch_stamp_is_refused(self):
        now = time.time()
        make_container(self.root, "AAAA", DRIVER, ready_at=now - 500)
        self.assertIsNone(session.find_channel(root=self.root, newer_than=now - 100))

    def test_container_without_ready_is_ignored(self):
        make_container(self.root, "AAAA", DRIVER, ready_at=None)
        self.assertIsNone(session.find_channel(root=self.root))


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.channel = tempfile.mkdtemp(prefix="uiflow-channel-")
        self.addCleanup(shutil.rmtree, self.channel, True)

    def beat(self, age=0.0):
        path = os.path.join(self.channel, "heartbeat")
        with open(path, "w") as handle:
            handle.write("1")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def test_fresh_heartbeat_is_live(self):
        self.beat()
        self.assertTrue(session.channel_is_live(self.channel))

    def test_stale_heartbeat_is_dead(self):
        self.beat(age=session.HEARTBEAT_STALE_SECONDS + 30)
        self.assertFalse(session.channel_is_live(self.channel))

    def test_momentarily_absent_heartbeat_is_retried(self):
        calls = []

        def fake_sleep(seconds):
            calls.append(seconds)
            if len(calls) == 1:
                self.beat()

        self.assertTrue(session.channel_is_live(self.channel, sleep=fake_sleep))
        self.assertEqual(len(calls), 1)

    def test_permanently_absent_heartbeat_is_dead_after_retries(self):
        calls = []
        self.assertFalse(session.channel_is_live(self.channel, sleep=calls.append))
        self.assertTrue(len(calls) >= 1)


class SimulatorGuardTests(unittest.TestCase):
    def setUp(self):
        self.saved = session._list_devices
        self.addCleanup(setattr, session, "_list_devices", self.saved)

    def install(self, devices):
        session._list_devices = lambda: devices

    def test_non_simulator_udid_raises(self):
        self.install({"devices": {"iOS 26.5": [{"name": "uiflow", "udid": "SIM-1", "isAvailable": True}]}})
        with self.assertRaises(session.SessionError) as caught:
            session.assert_simulator("00008130-AABBCCDDEEFF0011")
        self.assertIn("simulator", str(caught.exception).lower())

    def test_simulator_udid_passes(self):
        self.install({"devices": {"iOS 26.5": [{"name": "uiflow", "udid": "SIM-1", "isAvailable": True}]}})
        self.assertEqual(session.assert_simulator("SIM-1"), "SIM-1")

    def test_up_refuses_a_non_simulator_udid_before_launching(self):
        self.install({"devices": {"iOS 26.5": [{"name": "uiflow", "udid": "SIM-1", "isAvailable": True}]}})
        launched = []
        saved = session._launch_driver
        session._launch_driver = lambda *a, **k: launched.append(a)
        self.addCleanup(setattr, session, "_launch_driver", saved)
        with self.assertRaises(session.SessionError):
            session.up(udid="00008130-AABBCCDDEEFF0011")
        self.assertEqual(launched, [])


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-state-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.saved = session.paths.STATE_DIR
        session.paths.STATE_DIR = self.dir
        self.addCleanup(setattr, session.paths, "STATE_DIR", self.saved)

    def test_round_trip(self):
        session.write_state("SIM-1", {"pid": 42, "channel": "/tmp/x"})
        self.assertEqual(session.read_state("SIM-1")["pid"], 42)

    def test_missing_state_is_none(self):
        self.assertIsNone(session.read_state("SIM-NOPE"))

    def test_corrupt_state_is_none(self):
        with open(os.path.join(self.dir, "session-SIM-1.json"), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(session.read_state("SIM-1"))


class DriverPidSelectionTests(unittest.TestCase):
    """Reaping is what stops a dying runner from owning the simulator the next one needs.

    Every ui-flow driver on the machine carries the same two substrings in its command line, so a
    filter that looks only at those SIGTERMs other sessions' drivers on other simulators — a
    failure nobody sees until it destroys somebody else's run.
    """

    MINE = (
        "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild test-without-building "
        "-xctestrun ~/.ui-flow/cache/driver-build/Build/Products/UIFlowDriver_UIFlowDriver.xctestrun "
        "-destination platform=iOS Simulator,id=AAAA-1111"
    )
    THEIRS = MINE.replace("AAAA-1111", "BBBB-2222")
    UNRELATED = "/usr/bin/xcodebuild test -scheme Slider -destination id=AAAA-1111"

    def setUp(self):
        self.commands = {}
        saved = session.subprocess.run
        self.addCleanup(setattr, session.subprocess, "run", saved)
        session.subprocess.run = self.fake_run

    def fake_run(self, argv, **kwargs):
        class Result:
            pass

        result = Result()
        if argv[0] == "pgrep":
            result.stdout = "\n".join(sorted(self.commands, key=int)) + "\n"
        else:
            result.stdout = self.commands.get(argv[-1], "")
        return result

    def test_only_the_named_simulators_driver_is_selected(self):
        self.commands = {"101": self.MINE, "202": self.THEIRS}
        self.assertEqual(session._driver_xcodebuild_pids("AAAA-1111"), [101])

    def test_a_non_driver_xcodebuild_on_the_same_simulator_is_left_alone(self):
        self.commands = {"303": self.UNRELATED}
        self.assertEqual(session._driver_xcodebuild_pids("AAAA-1111"), [])

    def test_without_a_udid_every_driver_is_selected(self):
        self.commands = {"101": self.MINE, "202": self.THEIRS}
        self.assertEqual(session._driver_xcodebuild_pids(), [101, 202])

    def test_clearing_a_stale_runner_signals_only_that_simulator(self):
        """The call site is the fix. Pinning only the helper let the udid be dropped here silently."""
        self.commands = {"101": self.MINE, "202": self.THEIRS}
        signalled = []
        saved = session.os.kill
        self.addCleanup(setattr, session.os, "kill", saved)
        session.os.kill = lambda pid, sig: signalled.append(pid)
        session._clear_stale_runner("AAAA-1111")
        self.assertEqual(signalled, [101])



class SessionFixture(unittest.TestCase):
    """Shared plumbing: an isolated lease dir, an isolated state dir, and no real subprocesses."""

    def setUp(self):
        self.locks = tempfile.mkdtemp(prefix="uiflow-lease-")
        self.addCleanup(shutil.rmtree, self.locks, True)
        self.state = tempfile.mkdtemp(prefix="uiflow-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.set_env("UIFLOW_LOCK_DIR", self.locks)
        self.set_env("UIFLOW_OWNER", "session-a")
        self.swap(session.paths, "STATE_DIR", self.state)
        self.swap(session, "_list_devices",
                  lambda: {"devices": {"iOS 26.5": [{"name": "uiflow-iphone17pro",
                                                     "udid": "SIM-1", "isAvailable": True}]}})
        self.cleared = []
        self.swap(session, "_clear_stale_runner", self.cleared.append)
        self.swap(session, "find_channel", lambda *a, **k: None)
        self.swap(session, "xctestrun_path", lambda: "/tmp/none.xctestrun")
        self.launched = []
        self.swap(session, "_launch_driver", lambda *a, **k: self.launched.append(a) or 4242)
        self.swap(session, "_pid_alive", lambda pid: False)

    def swap(self, target, name, value):
        saved = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, saved)

    def set_env(self, key, value):
        saved = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.__setitem__(key, saved) if saved is not None
                        else os.environ.pop(key, None))

class OwnershipTests(SessionFixture):
    """`up` may share a live driver, but it must never kill one another session is holding."""

    def test_up_holds_the_lease_while_it_starts_the_driver(self):
        seen = []
        self.swap(session, "_launch_driver",
                  lambda *a, **k: seen.append(session.lease.read("SIM-1")) or 4242)
        with self.assertRaises(session.SessionError):
            session.up(udid="SIM-1", timeout=0.01)
        self.assertEqual(seen[0]["owner"], "session-a")
        self.assertEqual(self.cleared, ["SIM-1"])

    def test_a_failed_up_releases_the_lease_it_took(self):
        # Otherwise an unbuilt driver locks every other session out of a simulator that has
        # nothing running on it.
        with self.assertRaises(session.SessionError):
            session.up(udid="SIM-1", timeout=0.01)
        self.assertIsNone(session.lease.read("SIM-1"))

    def test_a_failed_up_releases_the_lease_when_the_driver_is_unbuilt(self):
        self.swap(session, "xctestrun_path",
                  lambda: (_ for _ in ()).throw(session.SessionError("no .xctestrun")))
        with self.assertRaises(session.SessionError):
            session.up(udid="SIM-1", timeout=0.01)
        self.assertIsNone(session.lease.read("SIM-1"))

    def test_up_refuses_a_simulator_another_session_is_holding(self):
        session.lease.acquire("SIM-1")
        self.set_env("UIFLOW_OWNER", "session-b")
        with self.assertRaises(session.lease.LeaseHeld) as caught:
            session.up(udid="SIM-1", timeout=0.01)
        self.assertIn("session-a", str(caught.exception))
        self.assertEqual(self.cleared, [], "it must not kill the other session's runner")
        self.assertEqual(self.launched, [])

    def test_steal_overrides_a_live_lease(self):
        session.lease.acquire("SIM-1")
        self.set_env("UIFLOW_OWNER", "session-b")
        seen = []
        self.swap(session, "_launch_driver",
                  lambda *a, **k: seen.append(session.lease.read("SIM-1")) or 4242)
        with self.assertRaises(session.SessionError):
            session.up(udid="SIM-1", timeout=0.01, steal=True)
        self.assertEqual(seen[0]["owner"], "session-b")

    def test_a_stale_lease_does_not_block(self):
        session.lease.acquire("SIM-1")
        session.lease.write("SIM-1", dict(session.lease.read("SIM-1"), renewedAt=0.0))
        self.set_env("UIFLOW_OWNER", "session-b")
        seen = []
        self.swap(session, "_launch_driver",
                  lambda *a, **k: seen.append(session.lease.read("SIM-1")) or 4242)
        with self.assertRaises(session.SessionError):
            session.up(udid="SIM-1", timeout=0.01)
        self.assertEqual(seen[0]["owner"], "session-b")

    def test_reusing_a_live_driver_never_consults_the_lease(self):
        session.lease.acquire("SIM-1")
        self.set_env("UIFLOW_OWNER", "session-b")
        self.swap(session, "find_channel", lambda *a, **k: "/tmp/chan")
        self.swap(session, "channel_is_live", lambda *a, **k: True)
        state = session.up(udid="SIM-1")
        self.assertTrue(state["reused"])
        self.assertEqual(self.cleared, [])
        self.assertEqual(session.lease.read("SIM-1")["owner"], "session-a",
                         "sharing a warm driver must not reassign the lease")


class DriverFeatureTests(SessionFixture):
    """The driver build is cached OUTSIDE the checkout, so a stale one silently ignores labelAnyOf:
    zero predicates, `element(boundBy: 0)`, a tap on the first button and a vacuous assert."""

    def answer(self, info):
        from uiflow import channel as channel_client
        self.swap(channel_client, "send_to",
                  lambda directory, steps, **kwargs: {"ok": True, "results": [{"info": info}]})

    def test_a_ping_reply_names_what_the_driver_supports(self):
        self.answer("features=labelAnyOf,labelContainsAnyOf")
        self.assertEqual(session.probe_features("/tmp/chan"), ["labelAnyOf", "labelContainsAnyOf"])

    def test_a_driver_that_advertises_nothing_supports_nothing(self):
        self.answer(None)
        self.assertEqual(session.probe_features("/tmp/chan"), [])

    def test_an_unanswerable_ping_is_no_answer_and_is_never_cached_as_one(self):
        """A driver mid-batch cannot answer; caching that as `[]` would disable localization for
        the rest of the session."""
        from uiflow import channel as channel_client

        def explode(*args, **kwargs):
            raise channel_client.ChannelTimeout("no answer")

        self.swap(channel_client, "send_to", explode)
        self.assertIsNone(session.probe_features("/tmp/chan"))
        session.write_state("SIM-1", {"udid": "SIM-1", "channel": "/tmp/chan"})
        self.swap(session, "channel_is_live", lambda *args, **kwargs: True)
        session.up(udid="SIM-1")
        self.assertFalse(session.driver_supports("SIM-1", "labelAnyOf"))
        self.assertIsNone(session.read_state("SIM-1").get("features"))

    def test_adopting_a_found_channel_records_its_features_and_keeps_the_language(self):
        session.write_state("SIM-1", {"udid": "SIM-1", "uiLanguage": "ko-KR"})
        self.answer("features=labelAnyOf")
        self.swap(session, "find_channel", lambda *args, **kwargs: "/tmp/chan")
        self.swap(session, "channel_is_live", lambda *args, **kwargs: True)
        self.assertTrue(session.up(udid="SIM-1")["reused"])
        state = session.read_state("SIM-1")
        self.assertEqual(state["features"], ["labelAnyOf"])
        self.assertEqual(state["uiLanguage"], "ko-KR")

    def test_a_cached_session_that_never_recorded_features_learns_them_on_reuse(self):
        session.write_state("SIM-1", {"udid": "SIM-1", "channel": "/tmp/chan", "pid": 7})
        self.answer("features=labelAnyOf")
        self.swap(session, "channel_is_live", lambda *args, **kwargs: True)
        self.assertTrue(session.up(udid="SIM-1")["reused"])
        self.assertEqual(session.read_state("SIM-1")["features"], ["labelAnyOf"])
        self.assertTrue(session.driver_supports("SIM-1", "labelAnyOf"))

    def test_a_cached_session_that_already_answered_is_not_pinged_again(self):
        session.write_state("SIM-1", {"udid": "SIM-1", "channel": "/tmp/chan", "features": []})
        pinged = []
        self.swap(session, "probe_features", lambda channel: pinged.append(channel) or ["labelAnyOf"])
        self.swap(session, "channel_is_live", lambda *args, **kwargs: True)
        session.up(udid="SIM-1")
        self.assertEqual(pinged, [])
        self.assertFalse(session.driver_supports("SIM-1", "labelAnyOf"))

    def test_a_simulator_with_no_session_state_supports_nothing(self):
        self.assertFalse(session.driver_supports("SIM-1", "labelAnyOf"))


class DownOwnershipTests(SessionFixture):
    """The driver idles for 1800s but a lease goes stale at 600s, so a healthy driver spends 20
    minutes looking abandoned. `down` must go by liveness, not by lease freshness."""

    def setUp(self):
        super().setUp()
        from uiflow import channel as channel_client

        self.quit = []
        self.swap(session, "find_channel", lambda *a, **k: "/chan")
        self.swap(session, "channel_is_live", lambda *a, **k: True)
        self.swap(session, "read_state", lambda udid: {"channel": "/chan", "pid": None})
        self.swap(channel_client, "send_to",
                  lambda channel, steps, timeout=None: self.quit.append(steps))

    def test_down_refuses_a_live_driver_whose_lease_merely_went_stale(self):
        session.lease.write("SIM-1", {"udid": "SIM-1", "owner": "session-a", "renewedAt": 0.0})
        self.set_env("UIFLOW_OWNER", "session-b")
        with self.assertRaises(session.lease.LeaseHeld):
            session.down(udid="SIM-1")
        self.assertEqual(self.quit, [], "it must not send quit to the other session's driver")

    def test_down_refuses_a_fresh_foreign_lease(self):
        session.lease.acquire("SIM-1")
        self.set_env("UIFLOW_OWNER", "session-b")
        with self.assertRaises(session.lease.LeaseHeld):
            session.down(udid="SIM-1")
        self.assertEqual(self.quit, [])

    def test_steal_takes_it_down_anyway(self):
        session.lease.write("SIM-1", {"udid": "SIM-1", "owner": "session-a", "renewedAt": 0.0})
        self.set_env("UIFLOW_OWNER", "session-b")
        session.down(udid="SIM-1", steal=True)
        self.assertEqual(self.quit, [[{"op": "quit"}]])

    def test_our_own_driver_goes_down_without_ceremony(self):
        session.lease.acquire("SIM-1")
        session.down(udid="SIM-1")
        self.assertEqual(self.quit, [[{"op": "quit"}]])


class DownWithoutALeaseTests(SessionFixture):
    """`up` releases its lease when the ready poll times out, but the detached xcodebuild can
    publish its channel moments later — leaving a healthy driver nobody holds a claim on."""

    def setUp(self):
        super().setUp()
        from uiflow import channel as channel_client

        self.quit = []
        self.swap(session, "find_channel", lambda *a, **k: "/chan")
        self.swap(session, "channel_is_live", lambda *a, **k: True)
        self.swap(channel_client, "send_to",
                  lambda channel, steps, timeout=None: self.quit.append(steps))

    def test_a_live_driver_we_neither_started_nor_hold_is_not_quit(self):
        self.swap(session, "read_state", lambda udid: {})
        with self.assertRaises(session.lease.LeaseHeld):
            session.down(udid="SIM-1")
        self.assertEqual(self.quit, [])

    def test_our_own_started_driver_still_goes_down(self):
        self.swap(session, "read_state", lambda udid: {"channel": "/chan", "pid": 4242})
        session.down(udid="SIM-1")
        self.assertEqual(self.quit, [[{"op": "quit"}]])

    def test_steal_clears_the_lease_it_stole_past(self):
        # Otherwise the stolen lease outlives the driver and the next `up` needs --steal too.
        self.swap(session, "read_state", lambda udid: {"channel": "/chan", "pid": 4242})
        session.lease.acquire("SIM-1")
        self.set_env("UIFLOW_OWNER", "session-b")
        session.down(udid="SIM-1", steal=True)
        self.assertIsNone(session.lease.read("SIM-1"),
                          "down --steal must not leave the other session's lease behind")


if __name__ == "__main__":
    unittest.main()
