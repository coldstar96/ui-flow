import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import metrics

UDID = "1111-2222"
IPHONE = {"w": 402.0, "h": 874.0, "density": 3.0}


class FingerprintTests(unittest.TestCase):
    def test_rounds_to_whole_points(self):
        self.assertEqual(metrics.fingerprint(402.0, 874.0), "402x874")

    def test_rounds_a_fractional_measurement(self):
        self.assertEqual(metrics.fingerprint(401.6, 873.8), "402x874")

    def test_landscape_and_portrait_share_one_fingerprint(self):
        """idb reports the hardware screen and the driver the current interface orientation, so the
        same device must not hash two ways depending on which channel is live."""
        self.assertEqual(metrics.fingerprint(874, 402), "402x874")
        self.assertEqual(metrics.fingerprint(402, 874), metrics.fingerprint(874, 402))


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        metrics.invalidate()
        self.addCleanup(metrics.invalidate)
        for name in ("probe_idb", "probe_driver"):
            saved = getattr(metrics, name)
            self.addCleanup(setattr, metrics, name, saved)
        saved_describe = metrics.paths.describe
        self.addCleanup(setattr, metrics.paths, "describe", saved_describe)
        metrics.paths.describe = lambda udid, listing=None: {"deviceType": "iPhone 17 Pro"}

    def patch(self, idb_result, driver_result):
        metrics.probe_idb = lambda udid: idb_result
        metrics.probe_driver = lambda udid: driver_result

    def test_idb_is_preferred_and_reports_points(self):
        self.patch(dict(IPHONE), None)
        found = metrics.screen(UDID)
        self.assertEqual(found["fingerprint"], "402x874")
        self.assertEqual(found["density"], 3.0)
        self.assertEqual(found["source"], metrics.SOURCE_IDB)

    def test_the_device_name_comes_from_the_simulator_listing(self):
        self.patch(dict(IPHONE), None)
        self.assertEqual(metrics.screen(UDID)["device"], "iPhone 17 Pro")

    def test_an_unlistable_udid_still_resolves_with_no_device_name(self):
        metrics.paths.describe = lambda udid, listing=None: None
        self.patch(dict(IPHONE), None)
        self.assertIsNone(metrics.screen(UDID)["device"])

    def test_the_driver_answers_when_idb_does_not(self):
        self.patch(None, {"w": 1024.0, "h": 1366.0, "density": None})
        found = metrics.screen(UDID)
        self.assertEqual(found["fingerprint"], "1024x1366")
        self.assertEqual(found["source"], metrics.SOURCE_DRIVER)

    def test_neither_channel_live_raises_naming_both_commands(self):
        self.patch(None, None)
        with self.assertRaises(metrics.MetricsUnavailable) as caught:
            metrics.screen(UDID)
        self.assertIn("uiflow up", str(caught.exception))
        self.assertIn("uiflow idb up", str(caught.exception))

    def test_no_udid_raises_metrics_unavailable(self):
        with self.assertRaises(metrics.MetricsUnavailable):
            metrics.screen(None)

    def test_an_empty_udid_raises_metrics_unavailable(self):
        with self.assertRaises(metrics.MetricsUnavailable):
            metrics.screen("")

    def test_a_zero_sized_reply_is_treated_as_no_answer(self):
        self.patch({"w": 0.0, "h": 0.0, "density": None}, None)
        with self.assertRaises(metrics.MetricsUnavailable):
            metrics.screen(UDID)

    def test_a_resolved_value_is_cached_within_the_process(self):
        calls = []
        metrics.probe_idb = lambda udid: (calls.append(udid), dict(IPHONE))[1]
        metrics.probe_driver = lambda udid: None
        metrics.screen(UDID)
        metrics.screen(UDID)
        self.assertEqual(len(calls), 1)

    def test_refresh_forces_a_new_probe(self):
        calls = []
        metrics.probe_idb = lambda udid: (calls.append(udid), dict(IPHONE))[1]
        metrics.probe_driver = lambda udid: None
        metrics.screen(UDID)
        metrics.screen(UDID, refresh=True)
        self.assertEqual(len(calls), 2)

    def test_a_landscape_driver_reading_hits_the_portrait_keymap(self):
        self.patch(None, {"w": 874.0, "h": 402.0, "density": None})
        found = metrics.screen(UDID)
        self.assertEqual(found["fingerprint"], "402x874")
        self.assertEqual((found["w"], found["h"]), (874.0, 402.0))

    def test_invalidate_drops_only_the_named_udid(self):
        self.patch(dict(IPHONE), None)
        metrics.screen(UDID)
        metrics.screen("other")
        metrics.invalidate(UDID)
        self.assertNotIn(UDID, metrics._CACHE)
        self.assertIn("other", metrics._CACHE)


class ProbeFailureTests(unittest.TestCase):
    """A probe is best-effort: whatever it raises, the caller must still see MetricsUnavailable."""

    def setUp(self):
        metrics.invalidate()
        self.addCleanup(metrics.invalidate)
        for holder, name in ((metrics.idb, "alive"), (metrics.idb, "send"),
                             (metrics.channel, "resolve_channel"), (metrics.channel, "send")):
            self.addCleanup(setattr, holder, name, getattr(holder, name))
        metrics.idb.alive = lambda udid: False
        metrics.channel.resolve_channel = lambda udid: None

    def test_an_idb_probe_that_times_out_is_not_an_answer(self):
        def boom(*args, **kwargs):
            raise TimeoutError("timed out")
        metrics.idb.alive = lambda udid: True
        metrics.idb.send = boom
        self.assertIsNone(metrics.probe_idb(UDID))
        with self.assertRaises(metrics.MetricsUnavailable):
            metrics.screen(UDID)

    def test_a_driver_probe_that_raises_is_not_an_answer(self):
        def boom(*args, **kwargs):
            raise RuntimeError("the channel went away")
        metrics.channel.resolve_channel = boom
        self.assertIsNone(metrics.probe_driver(UDID))
        with self.assertRaises(metrics.MetricsUnavailable):
            metrics.screen(UDID)

    def test_the_swallowed_reason_reaches_the_caller(self):
        """A missing idb venv or companion must not surface as a bare `no live channel`."""
        def boom(*args, **kwargs):
            raise OSError("no such file: idb_companion")
        metrics.idb.alive = lambda udid: True
        metrics.idb.send = boom
        with self.assertRaises(metrics.MetricsUnavailable) as caught:
            metrics.screen(UDID)
        self.assertIn("no such file: idb_companion", str(caught.exception))
        self.assertIn("uiflow idb up", str(caught.exception))

    def test_the_driver_reason_wins_over_the_earlier_idb_one(self):
        def idb_boom(*args, **kwargs):
            raise OSError("idb went away")
        def driver_boom(*args, **kwargs):
            raise RuntimeError("driver socket refused")
        metrics.idb.alive = lambda udid: True
        metrics.idb.send = idb_boom
        metrics.channel.resolve_channel = driver_boom
        with self.assertRaises(metrics.MetricsUnavailable) as caught:
            metrics.screen(UDID)
        self.assertIn("driver socket refused", str(caught.exception))
        self.assertNotIn("idb went away", str(caught.exception))

    def test_a_silent_absence_carries_no_reason(self):
        with self.assertRaises(metrics.MetricsUnavailable) as caught:
            metrics.screen(UDID)
        self.assertNotIn("the last probe failed", str(caught.exception))

    def test_a_driver_result_that_is_not_an_object_is_not_an_answer(self):
        metrics.channel.resolve_channel = lambda udid: "channel"
        metrics.channel.send = lambda ops, udid=None, timeout=None: {"ok": True, "results": ["no"]}
        self.assertIsNone(metrics.probe_driver(UDID))
        with self.assertRaises(metrics.MetricsUnavailable):
            metrics.screen(UDID)


if __name__ == "__main__":
    unittest.main()
