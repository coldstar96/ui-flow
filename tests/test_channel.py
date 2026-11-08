import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import channel


class FakeDriver(threading.Thread):
    def __init__(self, directory, replier, delay=0.0):
        threading.Thread.__init__(self, daemon=True)
        self.directory = directory
        self.replier = replier
        self.delay = delay
        self.stop = threading.Event()
        self.seen = []

    def run(self):
        command = os.path.join(self.directory, "cmd.json")
        while not self.stop.is_set():
            try:
                with open(command) as handle:
                    request = json.load(handle)
            except (OSError, ValueError):
                time.sleep(0.002)
                continue
            os.remove(command)
            self.seen.append(request)
            if self.delay:
                time.sleep(self.delay)
            reply = self.replier(request)
            if reply is not None:
                path = os.path.join(self.directory, "reply.json")
                tmp = path + ".tmp"
                with open(tmp, "w") as handle:
                    json.dump(reply, handle)
                os.replace(tmp, path)


def ok_reply(request):
    return {
        "seq": request["seq"],
        "ok": True,
        "elapsedMs": 3,
        "results": [{"index": i, "op": s.get("op"), "ms": 1, "ok": True} for i, s in enumerate(request["steps"])],
    }


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-chan-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = tempfile.mkdtemp(prefix="uiflow-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        saved = channel.paths.STATE_DIR
        channel.paths.STATE_DIR = self.state
        self.addCleanup(setattr, channel.paths, "STATE_DIR", saved)

    def serve(self, replier=ok_reply, delay=0.0):
        driver = FakeDriver(self.dir, replier, delay)
        driver.start()
        self.addCleanup(driver.stop.set)
        return driver

    def test_round_trip(self):
        driver = self.serve()
        started = time.time()
        reply = channel.send_to(self.dir, [{"op": "ping"}], timeout=5.0)
        self.assertTrue(reply["ok"])
        self.assertEqual(driver.seen[0]["steps"], [{"op": "ping"}])
        self.assertLess((time.time() - started) * 1000, 200)

    def test_request_carries_pace_and_default_app(self):
        driver = self.serve()
        channel.send_to(self.dir, [{"op": "ping"}], pace=2.0, default_app="com.apple.Preferences", timeout=5.0)
        self.assertEqual(driver.seen[0]["pace"], 2.0)
        self.assertEqual(driver.seen[0]["defaultApp"], "com.apple.Preferences")

    def test_stale_reply_from_an_earlier_seq_is_ignored(self):
        path = os.path.join(self.dir, "reply.json")
        with open(path, "w") as handle:
            json.dump({"seq": 1, "ok": False, "results": [], "error": {"reason": "stale"}}, handle)
        self.serve(delay=0.05)
        reply = channel.send_to(self.dir, [{"op": "ping"}], seq=7, timeout=5.0)
        self.assertEqual(reply["seq"], 7)
        self.assertTrue(reply["ok"])

    def test_timeout_raises(self):
        self.serve(replier=lambda request: None)
        with self.assertRaises(channel.ChannelTimeout):
            channel.send_to(self.dir, [{"op": "ping"}], timeout=0.3)

    def test_bad_request_reply_surfaces_its_reason(self):
        def replier(request):
            return {
                "seq": request["seq"],
                "ok": False,
                "results": [],
                "elapsedMs": 1,
                "error": {"reason": "badRequest", "detail": "unknown key", "failedPath": [], "snapshot": []},
            }

        self.serve(replier=replier)
        reply = channel.send_to(self.dir, [{"op": "nope"}], timeout=5.0)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["reason"], "badRequest")

    def test_seq_is_monotonic(self):
        first = channel.next_seq(self.dir)
        self.assertEqual(channel.next_seq(self.dir), first + 1)
        self.assertEqual(channel.next_seq(self.dir), first + 2)

    def test_the_counter_lives_beside_the_channel_so_every_checkout_shares_it(self):
        channel.next_seq(self.dir)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "seq")))

    def test_a_second_session_queues_rather_than_clobbering_the_request(self):
        with channel.held(self.dir, timeout=5.0):
            with self.assertRaises(channel.ChannelTimeout):
                with channel.held(self.dir, timeout=0.2):
                    self.fail("the lock let a second holder in")

    def test_snapshot_parses_json_lines_out_of_info(self):
        lines = [json.dumps({"app": "a", "type": "button", "label": 'say "hi"', "frame": [0, 0, 10, 10]})]

        def replier(request):
            return {
                "seq": request["seq"],
                "ok": True,
                "elapsedMs": 2,
                "results": [{"index": 0, "op": "snapshot", "ms": 2, "ok": True, "info": "\n".join(lines)}],
            }

        self.serve(replier=replier)
        elements = channel.snapshot_from(self.dir, app="com.apple.Preferences")
        self.assertEqual(elements[0]["label"], 'say "hi"')

    def test_missing_channel_raises_channel_error(self):
        with self.assertRaises(channel.ChannelError):
            channel.send([{"op": "ping"}], udid="SIM-NOPE", resolve=lambda udid: None)



class SeedSeqTests(unittest.TestCase):
    """A driver ignores any seq it has already served, so a fresh counter must not restart at 1."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-seed-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def reply(self, seq):
        with open(os.path.join(self.dir, "reply.json"), "w") as handle:
            json.dump({"seq": seq, "ok": True, "results": []}, handle)

    def test_a_new_counter_resumes_above_what_the_driver_already_served(self):
        self.reply(715)
        self.assertEqual(channel.next_seq(self.dir), 716)
        self.assertEqual(channel.next_seq(self.dir), 717)

    def test_a_fresh_driver_starts_at_one(self):
        self.assertEqual(channel.next_seq(self.dir), 1)

    def test_an_existing_counter_is_authoritative(self):
        self.reply(715)
        with open(os.path.join(self.dir, "seq"), "w") as handle:
            handle.write("900")
        self.assertEqual(channel.next_seq(self.dir), 901)

    def test_an_unreadable_reply_does_not_break_seeding(self):
        with open(os.path.join(self.dir, "reply.json"), "w") as handle:
            handle.write("{ not json")
        self.assertEqual(channel.next_seq(self.dir), 1)


if __name__ == "__main__":
    unittest.main()
