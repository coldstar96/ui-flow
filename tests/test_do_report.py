import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import localize, report, timing
from uiflow.commands import do

RESOLVED_SIM = {"udid": "RESOLVED-UDID", "name": "uiflow-iphone17pro",
                "deviceType": "iPhone 17 Pro", "runtime": "iOS 26.5", "state": "Booted"}


@contextlib.contextmanager
def resolving_sim():
    saved = (do.paths.default_udid, do.paths.describe, do.paths.list_devices)
    do.paths.default_udid = lambda listing=None: "RESOLVED-UDID"
    do.paths.describe = lambda udid, listing=None: dict(RESOLVED_SIM, udid=udid)
    do.paths.list_devices = lambda: {"devices": {}}
    try:
        yield
    finally:
        do.paths.default_udid, do.paths.describe, do.paths.list_devices = saved


def record(name, **extra):
    payload = {
        "app": "com.example.Demo",
        "name": name,
        "description": "",
        "verifiesWhy": "",
        "steps": [{"op": "tap", "sel": {"type": "button", "id": "go"}}],
        "timing": {},
        "health": {},
    }
    payload.update(extra)
    return payload


def parse(argv):
    parser = argparse.ArgumentParser()
    do.add_arguments(parser)
    return parser.parse_args(argv)


def shot_result(index, path="/nope/missing.png"):
    return {"index": index, "op": "screenshot", "ms": 200, "ok": True, "info": "path=%s" % path}


def group_result(index, name, ok=True):
    return {"index": index, "op": "group", "name": name, "ms": 500, "ok": ok}


class BuildBatchTests(unittest.TestCase):
    def test_capture_interleaves_screenshots_and_reports_record_positions(self):
        steps, positions, captions, origins = do.build_batch([record("a"), record("b")], capture=True)
        self.assertEqual([step["op"] for step in steps],
                         ["screenshot", "group", "screenshot", "group", "screenshot"])
        self.assertEqual(positions, [1, 3])
        self.assertEqual(captions[0], "before")

    def test_without_capture_the_batch_is_exactly_the_groups(self):
        steps, positions, captions, origins = do.build_batch([record("a"), record("b")], capture=False)
        self.assertEqual([step["op"] for step in steps], ["group", "group"])
        self.assertEqual(positions, [0, 1])
        self.assertEqual(captions, {})


class LearningIndexTests(unittest.TestCase):
    def test_learning_reads_each_record_result_through_its_batch_position(self):
        resolved = [record("a"), record("b")]
        _, positions, _, _ = do.build_batch(resolved, capture=True)
        reply = {"ok": True, "results": [
            shot_result(0), group_result(1, "a"), shot_result(2), group_result(3, "b"), shot_result(4),
        ]}
        saved = []
        do.apply_learning(resolved, reply, save=saved.append, positions=positions)
        self.assertEqual([entry["name"] for entry in saved], ["a", "b"])
        self.assertEqual([entry["health"]["runs"] for entry in saved], [1, 1])
        self.assertTrue(all(entry["health"].get("failures", 0) == 0 for entry in saved))

    def test_a_record_the_batch_never_reached_is_not_credited_with_a_run(self):
        resolved = [record("a"), record("b")]
        _, positions, _, _ = do.build_batch(resolved, capture=True)
        reply = {"ok": False, "results": [shot_result(0), group_result(1, "a", ok=False)]}
        saved = []
        do.apply_learning(resolved, reply, save=saved.append, positions=positions)
        self.assertEqual([entry["name"] for entry in saved], ["a"])
        self.assertEqual(saved[0]["health"]["failures"], 1)

    def test_without_positions_the_mapping_stays_one_to_one(self):
        resolved = [record("a")]
        saved = []
        do.apply_learning(resolved, {"ok": True, "results": [group_result(0, "a")]}, save=saved.append)
        self.assertEqual(saved[0]["health"]["runs"], 1)


class RepairIndexTests(unittest.TestCase):
    def reply(self, failed_path):
        return {"ok": False, "results": [], "error": {
            "reason": "notFound", "detail": "gone", "failedPath": failed_path,
            "snapshot": [json.dumps({"type": "button", "id": "go2", "label": "Go",
                                     "app": "com.example.Demo"})],
        }}

    def test_repair_targets_the_record_owning_the_failed_batch_slot(self):
        resolved = [record("a"), record("b")]
        _, positions, _, _ = do.build_batch(resolved, capture=True)
        saved = []
        patched = do.repair_once(
            resolved, self.reply([3, 0]),
            probe=lambda steps, **kw: {"ok": True, "results": [{"index": 0, "ok": True, "info": "index=0"}]},
            save=saved.append, auto_repair=True, echo=lambda line: None, positions=positions,
        )
        self.assertTrue(patched)
        self.assertEqual(saved[0]["name"], "b")
        self.assertIs(resolved[1], saved[0])

    def test_a_failure_in_a_screenshot_slot_is_never_repaired(self):
        resolved = [record("a")]
        _, positions, _, _ = do.build_batch(resolved, capture=True)
        patched = do.repair_once(
            resolved, self.reply([2, 0]),
            probe=lambda *a, **kw: self.fail("must not probe"),
            save=lambda payload: None, auto_repair=True, echo=lambda line: None, positions=positions,
        )
        self.assertIsNone(patched)


class FailureShotTests(unittest.TestCase):
    def test_captures_the_failing_screen(self):
        shots = do.failure_shot(send=lambda steps, **kw: {"ok": True, "results": [shot_result(0, "/tmp/f.png")]})
        self.assertEqual(shots, [{"caption": "FAILURE", "path": "/tmp/f.png", "keep": True}])

    def test_a_wedged_session_costs_nothing(self):
        def boom(steps, **kw):
            raise RuntimeError("channel gone")

        self.assertEqual(do.failure_shot(send=boom), [])

    def test_a_refused_screenshot_yields_nothing(self):
        self.assertEqual(do.failure_shot(send=lambda steps, **kw: {"ok": False}), [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.environ["UIFLOW_REPORT_ROOT"] = self.root
        self.sent = []
        self.resolved = [record("a")]
        self._resolve = do.records.resolve
        self._send = do.channel.send
        do.records.resolve = lambda app, names: [dict(entry) for entry in self.resolved]
        do.channel.send = self.fake_send
        self._time = do.time.time
        do.time.time = lambda: self.started
        self.reply = {"ok": True, "results": [shot_result(0), group_result(1, "a"), shot_result(2)]}
        # No test here needs a real device: without this the suite shells out to `xcrun` and reads
        # the machine's own lease directory, and its output depends on which simulators exist.
        self._sim = resolving_sim()
        self._sim.__enter__()
        self.addCleanup(self._sim.__exit__, None, None, None)

    def tearDown(self):
        do.records.resolve = self._resolve
        do.channel.send = self._send
        do.time.time = self._time
        os.environ.pop("UIFLOW_REPORT_ROOT", None)
        shutil.rmtree(self.root, ignore_errors=True)

    def fake_send(self, steps, **kwargs):
        self.sent.append(steps)
        return self.reply

    started = 1_700_000_000.0

    def reports(self):
        return [
            os.path.join(self.root, name, "report.html")
            for name in os.listdir(self.root)
            if os.path.exists(os.path.join(self.root, name, "report.html"))
        ]

    def test_a_default_run_captures_screenshots_and_writes_one_report(self):
        self.assertEqual(do.run(parse(["com.example.Demo", "a", "--no-learn"])), 0)
        self.assertEqual([step["op"] for step in self.sent[0]], ["screenshot", "group", "screenshot"])
        written = self.reports()
        self.assertEqual(len(written), 1)
        with open(written[0]) as handle:
            self.assertIn("PASS", handle.read())

    def test_the_report_records_when_the_run_started_not_when_it_was_written(self):
        """startedAt exists to name the run's start. Left unwired it silently records report-write
        time instead, which reads as correct and is not."""
        do.run(parse(["com.example.Demo", "a", "--no-learn"]))
        directory = os.path.dirname(self.reports()[0])
        with open(os.path.join(directory, report.SIDECAR_NAME)) as handle:
            started_at = json.load(handle)["startedAt"]
        self.assertEqual(started_at, report.now_iso(self.started))

    def test_no_report_leaves_the_batch_and_the_disk_untouched(self):
        self.assertEqual(do.run(parse(["com.example.Demo", "a", "--no-learn", "--no-report"])), 0)
        self.assertEqual([step["op"] for step in self.sent[0]], ["group"])
        self.assertEqual(self.reports(), [])

    def test_dry_run_neither_captures_nor_reports(self):
        self.assertEqual(do.run(parse(["com.example.Demo", "a", "--dry-run"])), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.reports(), [])

    def test_report_dir_overrides_the_default_location(self):
        target = os.path.join(self.root, "chosen")
        do.run(parse(["com.example.Demo", "a", "--no-learn", "--report-dir", target]))
        self.assertTrue(os.path.exists(os.path.join(target, "report.html")))

    def test_a_failing_run_reports_and_still_exits_nonzero(self):
        self.reply = {"ok": False, "results": [shot_result(0), group_result(1, "a", ok=False)],
                      "error": {"reason": "notFound", "detail": "gone", "failedPath": [1, 0],
                                "snapshot": []}}
        self.assertEqual(do.run(parse(["com.example.Demo", "a", "--no-learn", "--no-repair"])), 1)
        with open(self.reports()[0]) as handle:
            body = handle.read()
        self.assertIn("FAIL", body)
        self.assertIn("notFound", body)

    def test_a_report_that_cannot_be_written_does_not_fail_the_run(self):
        original = report.write
        report.write = lambda context, directory: (_ for _ in ()).throw(OSError("disk full"))
        try:
            self.assertEqual(do.run(parse(["com.example.Demo", "a", "--no-learn"])), 0)
        finally:
            report.write = original

    def test_the_report_names_the_simulator_even_when_no_udid_was_passed(self):
        do.run(parse(["com.example.Demo", "a", "--no-learn"]))
        with open(self.reports()[0].replace("report.html", "run.json")) as handle:
            self.assertEqual(json.load(handle)["sim"], RESOLVED_SIM)

    def test_the_run_announces_the_simulator_before_the_batch_goes_out(self):
        import contextlib, io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            do.run(parse(["com.example.Demo", "a", "--no-learn", "--no-report"]))
        self.assertIn("sim uiflow-iphone17pro · iPhone 17 Pro · iOS 26.5 · Booted · RESOLVED-UDID",
                      out.getvalue())

    def test_json_output_carries_the_report_path(self):
        import io

        buffer = io.StringIO()
        stdout, sys.stdout = sys.stdout, buffer
        try:
            do.run(parse(["com.example.Demo", "a", "--no-learn", "--json"]))
        finally:
            sys.stdout = stdout
        payload = json.loads(buffer.getvalue().strip().splitlines()[-1])
        self.assertTrue(payload["reportPath"].endswith("report.html"))

    def test_learning_that_cannot_be_written_does_not_fail_the_run(self):
        import io

        def refuse(*args, **kwargs):
            raise PermissionError("read-only caller directory")

        original = do.apply_learning
        do.apply_learning = refuse
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = do.run(parse(["com.example.Demo", "a", "--no-report"]))
        finally:
            do.apply_learning = original
        self.assertEqual(code, 0)
        self.assertIn("learning not saved: read-only caller directory", err.getvalue())
        self.assertIn("wallClockMs", out.getvalue())


class OwnerAttributionTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile

        from uiflow import records

        self.dir = tempfile.mkdtemp(prefix="uiflow-do-owner-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        self.app = "com.example.Demo"
        self.write("frag", [{"op": "tap", "sel": {"label": "x"}}], kind="fragment")
        self.write("caller", [{"op": "launch", "app": self.app}, {"op": "use", "ref": "frag"}])

    def write(self, name, steps, **extra):
        import json as json_

        from uiflow import records

        payload = {"app": self.app, "name": name, "steps": steps, "timing": {},
                   "health": {"runs": 0, "failures": 0, "lastOk": None, "repairedAt": None}}
        payload.update(extra)
        directory = os.path.join(self.dir, self.app)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json_.dump(payload, handle)
        records.invalidate()

    def test_build_batch_expands_use_and_returns_origins(self):
        from uiflow import records
        from uiflow.commands import do

        resolved = [records.load(self.app, "caller")]
        steps, positions, _, origins = do.build_batch(resolved, capture=False)
        self.assertEqual(steps[0]["steps"][1]["op"], "group")
        self.assertEqual(origins[0]["map"][(1, 0)], (records.qualify(self.app, "frag"), (0,)))
        self.assertEqual(origins[0]["owners"][(1,)], {"op": "use", "ref": "frag"})

    def test_learning_credits_the_fragment_not_the_caller(self):
        from uiflow import records, timing
        from uiflow.commands import do

        resolved = [records.load(self.app, "caller")]
        _, positions, _, origins = do.build_batch(resolved, capture=False)
        reply = {"ok": True, "results": [{
            "index": 0, "op": "group", "ms": 100, "ok": True,
            "children": [
                {"index": 0, "op": "launch", "ms": 30, "ok": True},
                {"index": 1, "op": "group", "ms": 60, "ok": True,
                 "children": [{"index": 0, "op": "tap", "ms": 50, "ok": True}]},
            ],
        }]}
        saved = {}
        do.apply_learning(resolved, reply, save=lambda r: saved.__setitem__(r["name"], r),
                          positions=positions, origins=origins)
        fragment, caller = saved["frag"], saved["caller"]
        self.assertEqual(timing.samples(fragment, [0], fragment["steps"][0]), [50])
        self.assertEqual(timing.samples(caller, [0], caller["steps"][0]), [30])
        self.assertEqual(caller["health"]["runs"], 1)
        self.assertEqual(fragment["health"]["runs"], 1)

    def test_the_owned_tree_is_walked_once_per_record(self):
        from uiflow import records, timing
        from uiflow.commands import do

        resolved = [records.load(self.app, "caller")]
        _, positions, _, origins = do.build_batch(resolved, capture=False)
        reply = {"ok": True, "results": [{
            "index": 0, "op": "group", "ms": 100, "ok": True,
            "children": [
                {"index": 0, "op": "launch", "ms": 30, "ok": True},
                {"index": 1, "op": "group", "ms": 60, "ok": True,
                 "children": [{"index": 0, "op": "tap", "ms": 50, "ok": True}]},
            ],
        }]}
        calls = []
        real = timing.collect_all

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        timing.collect_all = counting
        self.addCleanup(setattr, timing, "collect_all", real)
        do.apply_learning(resolved, reply, save=lambda r: None,
                          positions=positions, origins=origins)
        self.assertEqual(len(calls), 1)

    def test_failure_attribution_translates_the_batch_index(self):
        from uiflow import records
        from uiflow.commands import do

        resolved = [records.load(self.app, "caller")]
        _, positions, _, origins = do.build_batch(resolved, capture=True)
        self.assertEqual(positions, [1])
        reply = {"ok": False, "error": {"reason": "notFound", "failedPath": [1, 1, 0]}}
        self.assertEqual(do._failed_owner(reply, origins, positions),
                         records.qualify(self.app, "frag"))

    def test_a_failure_inside_a_fragment_credits_both_owners(self):
        from uiflow import records
        from uiflow.commands import do

        resolved = [records.load(self.app, "caller")]
        _, positions, _, origins = do.build_batch(resolved, capture=False)
        reply = {"ok": False,
                 "error": {"reason": "notFound", "failedPath": [0, 1, 0]},
                 "results": [{"index": 0, "op": "group", "ms": 10, "ok": False, "children": [
                     {"index": 0, "op": "launch", "ms": 5, "ok": True},
                     {"index": 1, "op": "group", "ms": 5, "ok": False, "children": [
                         {"index": 0, "op": "tap", "ms": 5, "ok": False}]},
                 ]}]}
        saved = {}
        do.apply_learning(resolved, reply, save=lambda r: saved.__setitem__(r["name"], r),
                          positions=positions, origins=origins)
        self.assertEqual(saved["frag"]["health"]["failures"], 1)
        self.assertEqual(saved["caller"]["health"]["failures"], 1)
        self.assertIsNone(saved["caller"]["health"]["lastOk"])

    def test_a_container_step_keeps_accumulating_samples_past_the_budget_threshold(self):
        from uiflow import records, timing
        from uiflow.commands import do

        self.write("loop", [{
            "op": "repeatUntil",
            "maxCycles": 3,
            "steps": [{"op": "tap", "sel": {"label": "x"}}],
            "until": [{"op": "assert", "sel": {"label": "done"}, "present": True}],
        }])
        for _ in range(6):
            resolved = [records.load(self.app, "loop")]
            _, positions, _, origins = do.build_batch(resolved, capture=False)
            reply = {"ok": True, "results": [{
                "index": 0, "op": "group", "ms": 120, "ok": True,
                "children": [{"index": 0, "op": "repeatUntil", "ms": 90, "ok": True,
                              "children": [{"index": 0, "op": "tap", "ms": 40, "ok": True}]}],
            }]}
            do.apply_learning(resolved, reply, positions=positions, origins=origins)
        stored = records.load(self.app, "loop")
        self.assertEqual(timing.samples(stored, [0], stored["steps"][0]), [90] * 6)
        self.assertEqual(timing.samples(stored, [0, 0], stored["steps"][0]["steps"][0]), [40] * 6)

    def test_a_fragment_that_ran_fine_is_not_charged_the_callers_failure(self):
        from uiflow import records
        from uiflow.commands import do

        self.write("caller2", [{"op": "use", "ref": "frag"}, {"op": "tap", "sel": {"label": "y"}}])
        resolved = [records.load(self.app, "caller2")]
        _, positions, _, origins = do.build_batch(resolved, capture=False)
        reply = {"ok": False,
                 "error": {"reason": "notFound", "failedPath": [0, 1]},
                 "results": [{"index": 0, "op": "group", "ms": 20, "ok": False, "children": [
                     {"index": 0, "op": "group", "ms": 10, "ok": True,
                      "children": [{"index": 0, "op": "tap", "ms": 8, "ok": True}]},
                     {"index": 1, "op": "tap", "ms": 5, "ok": False},
                 ]}]}
        saved = {}
        do.apply_learning(resolved, reply, save=lambda r: saved.__setitem__(r["name"], r),
                          positions=positions, origins=origins)
        self.assertEqual(saved["frag"]["health"]["failures"], 0)
        self.assertIsNotNone(saved["frag"]["health"]["lastOk"])
        self.assertEqual(saved["caller2"]["health"]["failures"], 1)

    def test_apply_learning_without_origins_is_unchanged(self):
        from uiflow import records, timing
        from uiflow.commands import do

        resolved = [records.load(self.app, "frag")]
        reply = {"ok": True, "results": [{
            "index": 0, "op": "group", "ms": 60, "ok": True,
            "children": [{"index": 0, "op": "tap", "ms": 40, "ok": True}],
        }]}
        saved = []
        do.apply_learning(resolved, reply, save=saved.append)
        self.assertEqual(timing.samples(saved[0], [0], saved[0]["steps"][0]), [40])



def _tap_record():
    steps = [{"op": "tapIfPresent", "sel": {"label": "a"}, "timeoutMs": 1800}]
    return {"app": "com.example.Demo", "name": "one", "steps": steps, "timing": {}}


def _absent_reply(ok=True, reason="notFound"):
    child = {"index": 0, "op": "tapIfPresent", "ms": 1800, "ok": True, "skipped": True}
    group = {"index": 0, "op": "group", "ms": 1900, "ok": ok, "children": [child]}
    payload = {"ok": ok, "results": [group]}
    if not ok:
        payload["error"] = {"reason": reason, "detail": "", "failedPath": [0, 0], "snapshot": []}
    return payload


class AbsentEvidenceTests(unittest.TestCase):
    def _key(self, saved):
        from uiflow import timing
        return timing.entry_key([0], saved["steps"][0])

    def test_an_absent_step_accrues_a_miss(self):
        from uiflow.commands import do
        saved = []
        do.apply_learning([_tap_record()], _absent_reply(), save=saved.append)
        self.assertEqual(saved[0]["timing"][self._key(saved[0])]["misses"], 1)

    def test_a_timeout_failure_resets_the_misses(self):
        from uiflow import timing
        from uiflow.commands import do
        record = _tap_record()
        record["timing"] = {timing.entry_key([0], record["steps"][0]): {"misses": 6}}
        saved = []
        do.apply_learning([record], _absent_reply(ok=False), save=saved.append)
        self.assertEqual(saved[0]["timing"][self._key(saved[0])]["misses"], 0)

    def test_a_non_timeout_failure_leaves_the_misses(self):
        from uiflow import timing
        from uiflow.commands import do
        record = _tap_record()
        record["timing"] = {timing.entry_key([0], record["steps"][0]): {"misses": 6}}
        saved = []
        do.apply_learning([record], _absent_reply(ok=False, reason="badStep"), save=saved.append)
        self.assertEqual(saved[0]["timing"][self._key(saved[0])]["misses"], 6)

    def test_a_repaired_failure_does_not_reset(self):
        from uiflow import timing
        from uiflow.commands import do
        record = _tap_record()
        record["timing"] = {timing.entry_key([0], record["steps"][0]): {"misses": 6}}
        saved = []
        do.apply_learning([record], _absent_reply(ok=False), save=saved.append, repaired=True)
        self.assertEqual(saved[0]["timing"][self._key(saved[0])]["misses"], 6)

class LocalizedBatchTests(unittest.TestCase):
    class FakeOracle:
        lang = "ko-US"
        closed = False

        def translate(self, label):
            return {"General": ["일반"]}.get(label) or []

        def originals(self, label):
            return ["General"] if label == "일반" else []

        def canonical(self, label):
            found = self.originals(label)
            return found[0] if len(found) == 1 else None

        def knows_english(self, label):
            return label == "General"

        def knows_localized(self, label):
            return label == "일반"

        def close(self):
            self.closed = True

    def record(self):
        return {"app": "com.apple.Preferences", "name": "walk", "satisfiedWhen": [], "timing": {},
                "steps": [{"op": "tap", "sel": {"type": "button", "label": "General"}}]}

    def test_labels_are_expanded_and_counted(self):
        steps, _, _, _ = do.build_batch([self.record()], False)
        localized, missing, seen = do.localize_batch(steps, self.FakeOracle())
        self.assertEqual(localized[0]["steps"][0]["sel"]["labelAnyOf"], ["General", "일반"])
        self.assertEqual((missing, seen), ([], 1))

    def test_without_an_oracle_the_batch_is_untouched(self):
        steps, _, _, _ = do.build_batch([self.record()], False)
        self.assertEqual(do.localize_batch(steps, None), (steps, [], 0))

    def test_timing_budgets_are_looked_up_before_localization(self):
        """A ko run reuses the en record's learned budgets rather than starting a second table."""
        record = self.record()
        record["timing"] = {timing.entry_key([0], record["steps"][0]): {"hits": [900, 900, 900]}}
        steps, _, _, _ = do.build_batch([record], False)
        localized, _, _ = do.localize_batch(steps, self.FakeOracle())
        self.assertEqual(localized[0]["steps"][0]["timeoutMs"], timing.budget_ms([900, 900, 900]))


class ResolveOracleTests(unittest.TestCase):
    """A driver built before `labelAnyOf` IGNORES it. With `label` deleted the query has ZERO
    predicates: the tap lands on the first button on screen and every assert passes vacuously."""

    def setUp(self):
        self.oracle = LocalizedBatchTests.FakeOracle()
        self.features = ["labelAnyOf", "labelContainsAnyOf"]
        self.swap(do.strings, "oracle", lambda *args, **kwargs: self.oracle)
        self.swap(do.session, "driver_supports",
                  lambda udid, feature: feature in self.features)

    def swap(self, module, name, value):
        saved = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, saved)

    def args(self, **overrides):
        return argparse.Namespace(**dict(
            {"app": "com.apple.Preferences", "no_localize": False}, **overrides))

    def test_a_driver_that_advertises_the_feature_localizes(self):
        self.assertIs(do.resolve_oracle(self.args(), "SIM-1", None), self.oracle)

    def test_a_stale_driver_replays_in_english_and_names_the_rebuild(self):
        self.features = []
        warned = []
        self.assertIsNone(do.resolve_oracle(self.args(), "SIM-1", None, echo=warned.append))
        self.assertTrue(self.oracle.closed, "the store it opened must not be left holding fds")
        self.assertIn("driver/build.sh", " ".join(warned))

    def test_an_english_simulator_is_not_warned_about_a_stale_driver(self):
        self.features = []
        self.oracle = None
        warned = []
        self.assertIsNone(do.resolve_oracle(self.args(), "SIM-1", None, echo=warned.append))
        self.assertEqual(warned, [])

    def test_no_localize_asks_the_driver_nothing_and_builds_nothing(self):
        asked = []
        self.swap(do.strings, "oracle", lambda *args, **kwargs: asked.append(1))
        self.assertIsNone(do.resolve_oracle(self.args(no_localize=True), "SIM-1", None))
        self.assertEqual(asked, [])



class UnrecognisedLabelTests(unittest.TestCase):
    """A repair the catalogue cannot place is still persisted — but it must say so."""

    class Oracle:
        lang = "es-ES"

        def knows_localized(self, label):
            return label == "Notificaciones"

        def knows_english(self, label):
            return label == "General"

    def test_text_in_neither_column_is_reported(self):
        found = do.unrecognised_labels({"type": "button", "label": "Ajustes"}, self.Oracle())
        self.assertEqual(found, ["Ajustes"])

    def test_a_known_translation_is_not_reported(self):
        self.assertEqual(
            do.unrecognised_labels({"type": "button", "label": "Notificaciones"}, self.Oracle()), [])

    def test_a_known_english_original_is_not_reported(self):
        self.assertEqual(
            do.unrecognised_labels({"type": "button", "label": "General"}, self.Oracle()), [])

    def test_a_descendant_is_reached(self):
        found = do.unrecognised_labels(
            {"type": "switch", "label": "General", "descendant": {"labelContains": "Ajustes"}},
            self.Oracle())
        self.assertEqual(found, ["Ajustes"])

    def test_no_oracle_reports_nothing(self):
        self.assertEqual(do.unrecognised_labels({"label": "Ajustes"}, None), [])


class OracleLifetimeTests(unittest.TestCase):
    """The oracle owns a sqlite handle. A trailing close is skipped by any raise above it."""

    class Oracle:
        lang = "ko"

        def __init__(self):
            self.closed = False

        def translate(self, label):
            return []

        def close(self):
            self.closed = True

    def setUp(self):
        self.oracle = self.Oracle()
        self.swap(do, "resolve_oracle", lambda *a, **k: self.oracle)
        self.swap(do.records, "resolve", lambda app, names: [
            {"app": app, "name": "a", "steps": [{"op": "tap", "sel": {"type": "button", "label": "x"}}],
             "satisfiedWhen": [], "timing": {}}])
        self._sim = resolving_sim()
        self._sim.__enter__()
        self.addCleanup(self._sim.__exit__, None, None, None)

    def swap(self, module, name, value):
        saved = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, saved)

    def test_the_oracle_is_closed_when_the_batch_raises(self):
        def boom(*a, **k):
            raise RuntimeError("channel died mid-batch")
        self.swap(do.channel, "send", boom)
        with self.assertRaises(RuntimeError):
            do.run(parse(["com.example.Demo", "a", "--no-learn", "--no-report"]))
        self.assertTrue(self.oracle.closed, "a raise above the close leaks the sqlite handle")

    def test_the_oracle_is_still_closed_on_a_clean_run(self):
        self.swap(do.channel, "send", lambda steps, **k: {"ok": True, "results": [{"status": "ok"}]})
        do.run(parse(["com.example.Demo", "a", "--no-learn", "--no-report"]))
        self.assertTrue(self.oracle.closed)

class ShapeWarningTests(unittest.TestCase):
    APP = "com.example.Demo"
    BARE_TAP = {"op": "tapAt", "dx": 0.5, "dy": 0.9}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-shape-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = do.records.paths.RECORDS_DIR
        do.records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, do.records.paths, "RECORDS_DIR", saved)
        do.records.invalidate()
        self.addCleanup(do.records.invalidate)
        saved_screen = do.metrics.screen
        self.addCleanup(setattr, do.metrics, "screen", saved_screen)
        do.metrics.screen = lambda udid, refresh=False: {"fingerprint": "402x874"}

    def write(self, name, steps, **extra):
        payload = {"app": self.APP, "name": name, "steps": steps}
        payload.update(extra)
        directory = os.path.join(self.dir, self.APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump(payload, handle)
        do.records.invalidate()
        return payload

    def caller_using(self, fragment_steps):
        self.write("frag", fragment_steps, kind="fragment")
        return self.write(
            "caller", [{"op": "use", "ref": "frag"}],
            recordedOn={"fingerprint": "440x956", "device": "iPhone 17 Pro Max"},
        )

    def test_a_bare_tap_at_inside_a_used_fragment_warns(self):
        caller = self.caller_using([self.BARE_TAP])
        lines = do.shape_warnings([caller], "UDID", self.APP)
        self.assertEqual(len(lines), 1)
        self.assertIn("440x956", lines[0])
        self.assertIn("402x874", lines[0])

    def test_a_fragment_whose_tap_at_has_a_sel_is_silent(self):
        caller = self.caller_using([dict(self.BARE_TAP, sel={"label": "Go"})])
        self.assertEqual(do.shape_warnings([caller], "UDID", self.APP), [])

    def test_the_same_device_never_warns(self):
        caller = self.caller_using([self.BARE_TAP])
        caller["recordedOn"] = {"fingerprint": "402x874", "device": "iPhone 17 Pro"}
        self.assertEqual(do.shape_warnings([caller], "UDID", self.APP), [])

    def test_a_landscape_ordered_stored_fingerprint_is_the_same_device(self):
        """`fingerprint` orders smaller-first now, so a value stored before that must not read as a
        different device."""
        caller = self.caller_using([self.BARE_TAP])
        caller["recordedOn"] = {"fingerprint": "874x402", "device": "iPhone 17 Pro"}
        self.assertEqual(do.shape_warnings([caller], "UDID", self.APP), [])

    def test_a_malformed_stored_fingerprint_is_silent(self):
        caller = self.caller_using([self.BARE_TAP])
        caller["recordedOn"] = {"fingerprint": "who knows", "device": "iPhone 17 Pro"}
        self.assertEqual(do.shape_warnings([caller], "UDID", self.APP), [])

    def test_the_warning_names_the_record_holding_the_tap_not_an_expanded_index(self):
        """`shape_dependent` indexes the EXPANDED steps, which is not a position in caller.json."""
        caller = self.caller_using([self.BARE_TAP])
        line = do.shape_warnings([caller], "UDID", self.APP)[0]
        self.assertIn("%s/frag" % self.APP, line)
        self.assertNotIn("step(s) 0.0", line)
        self.assertNotIn("step 0.0", line)

    def test_a_tap_in_the_top_record_is_attributed_to_it(self):
        caller = self.write(
            "solo", [self.BARE_TAP],
            recordedOn={"fingerprint": "440x956", "device": "iPhone 17 Pro Max"},
        )
        line = do.shape_warnings([caller], "UDID", self.APP)[0]
        self.assertIn("%s/solo" % self.APP, line)

    def test_a_satisfied_when_tap_is_attributed_to_its_own_record(self):
        caller = self.write(
            "guarded", [{"op": "tap", "sel": {"label": "Go"}}],
            satisfiedWhen=[self.BARE_TAP],
            recordedOn={"fingerprint": "440x956", "device": "iPhone 17 Pro Max"},
        )
        line = do.shape_warnings([caller], "UDID", self.APP)[0]
        self.assertIn("%s/guarded" % self.APP, line)
        self.assertNotIn("satisfiedWhen.0", line)

    def test_a_bare_tap_at_inside_a_fragments_reassert_warns_on_the_fragment(self):
        caller = self.caller_using([{
            "op": "waitFor", "sel": {"label": "Ready"}, "reassert": [self.BARE_TAP],
        }])
        line = do.shape_warnings([caller], "UDID", self.APP)[0]
        self.assertIn("%s/frag" % self.APP, line)
        self.assertNotIn("%s/caller" % self.APP, line)

    def test_a_bare_tap_at_inside_a_fragments_until_warns_on_the_fragment(self):
        caller = self.caller_using([{
            "op": "repeatUntil", "steps": [{"op": "tap", "sel": {"label": "x"}}],
            "until": [self.BARE_TAP],
        }])
        line = do.shape_warnings([caller], "UDID", self.APP)[0]
        self.assertIn("%s/frag" % self.APP, line)
        self.assertNotIn("%s/caller" % self.APP, line)

    def test_a_bare_tap_at_in_the_fragments_plain_steps_still_attributes_to_the_fragment(self):
        caller = self.caller_using([self.BARE_TAP])
        line = do.shape_warnings([caller], "UDID", self.APP)[0]
        self.assertIn("%s/frag" % self.APP, line)
        self.assertNotIn("%s/caller" % self.APP, line)

    def test_a_top_level_satisfied_when_tap_still_falls_back_to_its_own_record(self):
        caller = self.write(
            "guarded2", [{"op": "tap", "sel": {"label": "Go"}}],
            satisfiedWhen=[self.BARE_TAP],
            recordedOn={"fingerprint": "440x956", "device": "iPhone 17 Pro Max"},
        )
        line = do.shape_warnings([caller], "UDID", self.APP)[0]
        self.assertIn("%s/guarded2" % self.APP, line)


if __name__ == "__main__":
    unittest.main()

