import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import report

SIM = {"udid": "UDID-1", "name": "uiflow-iphone17pro", "deviceType": "iPhone 17 Pro",
       "runtime": "iOS 26.5", "state": "Booted"}

PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def group(name):
    return {"op": "group", "name": name, "steps": []}


def shot_result(index, path):
    return {"index": index, "op": "screenshot", "ms": 200, "ok": True, "info": "path=%s" % path}


def group_result(index, name, ok=True, skipped=False, ms=900):
    return {"index": index, "op": "group", "name": name, "ms": ms, "ok": ok, "skipped": skipped}


class PlanTests(unittest.TestCase):
    def test_interleaves_a_shot_before_the_run_and_after_every_group(self):
        steps, positions, captions = report.plan([group("a"), group("b")], ["a", "b"])
        self.assertEqual([step["op"] for step in steps],
                         ["screenshot", "group", "screenshot", "group", "screenshot"])
        self.assertEqual(positions, [1, 3])
        self.assertEqual(captions, {0: "before", 2: "after a", 4: "after b"})

    def test_empty_plan_still_captures_the_starting_screen(self):
        steps, positions, captions = report.plan([], [])
        self.assertEqual([step["op"] for step in steps], ["screenshot"])
        self.assertEqual(positions, [])
        self.assertEqual(captions, {0: "before"})

    def test_record_index_inverts_positions_and_rejects_shot_slots(self):
        _, positions, _ = report.plan([group("a"), group("b")], ["a", "b"])
        self.assertEqual(report.record_index(positions, 1), 0)
        self.assertEqual(report.record_index(positions, 3), 1)
        self.assertIsNone(report.record_index(positions, 0))
        self.assertIsNone(report.record_index(positions, 4))

    def test_plan_does_not_mutate_the_caller_groups(self):
        groups = [group("a")]
        report.plan(groups, ["a"])
        self.assertEqual(groups, [group("a")])


class ScreenshotPathTests(unittest.TestCase):
    def test_reads_the_path_token(self):
        self.assertEqual(report.screenshot_path({"info": "path=/tmp/shot-1.png"}), "/tmp/shot-1.png")

    def test_falls_back_to_bare_info(self):
        self.assertEqual(report.screenshot_path({"info": "/tmp/shot-2.png"}), "/tmp/shot-2.png")

    def test_missing_info_is_none(self):
        self.assertIsNone(report.screenshot_path({}))


class ShotsFromTests(unittest.TestCase):
    def test_pairs_each_shot_result_with_its_caption(self):
        _, _, captions = report.plan([group("a")], ["a"])
        reply = {"ok": True, "results": [
            shot_result(0, "/tmp/one.png"),
            group_result(1, "a"),
            shot_result(2, "/tmp/two.png"),
        ]}
        self.assertEqual(report.shots_from(reply, captions), [
            {"caption": "before", "path": "/tmp/one.png"},
            {"caption": "after a", "path": "/tmp/two.png"},
        ])

    def test_a_batch_that_aborted_early_yields_only_the_shots_that_ran(self):
        _, _, captions = report.plan([group("a"), group("b")], ["a", "b"])
        reply = {"ok": False, "results": [
            shot_result(0, "/tmp/one.png"),
            group_result(1, "a", ok=False),
        ]}
        self.assertEqual(report.shots_from(reply, captions),
                         [{"caption": "before", "path": "/tmp/one.png"}])

    def test_the_shot_after_a_skipped_record_folds_into_the_previous_screen(self):
        _, _, captions = report.plan([group("a"), group("b")], ["a", "b"])
        reply = {"ok": True, "results": [
            shot_result(0, "/tmp/one.png"),
            group_result(1, "a", skipped=True),
            shot_result(2, "/tmp/two.png"),
            group_result(3, "b"),
            shot_result(4, "/tmp/three.png"),
        ]}
        shots = report.shots_from(reply, captions)
        self.assertEqual([entry["caption"] for entry in shots], ["before", "after b"])
        self.assertEqual(shots[0]["also"], ["after a"])

    def test_consecutive_skips_all_fold_into_the_same_screen(self):
        _, _, captions = report.plan([group("a"), group("b")], ["a", "b"])
        reply = {"ok": True, "results": [
            shot_result(0, "/tmp/one.png"),
            group_result(1, "a", skipped=True),
            shot_result(2, "/tmp/two.png"),
            group_result(3, "b", skipped=True),
            shot_result(4, "/tmp/three.png"),
        ]}
        shots = report.shots_from(reply, captions)
        self.assertEqual([entry["caption"] for entry in shots], ["before"])
        self.assertEqual(shots[0]["also"], ["after a", "after b"])

    def test_a_record_whose_every_step_no_opped_folds_too(self):
        _, _, captions = report.plan([group("a")], ["a"])
        ran_nothing = dict(group_result(1, "a"), children=[
            {"index": 0, "op": "tapIfPresent", "ok": True, "skipped": True},
            {"index": 1, "op": "setSwitch", "ok": True, "skipped": True},
        ])
        reply = {"ok": True, "results": [
            shot_result(0, "/tmp/one.png"), ran_nothing, shot_result(2, "/tmp/two.png"),
        ]}
        shots = report.shots_from(reply, captions)
        self.assertEqual([entry["caption"] for entry in shots], ["before"])
        self.assertEqual(shots[0]["also"], ["after a"])

    def test_a_record_with_one_live_step_is_never_folded(self):
        _, _, captions = report.plan([group("a")], ["a"])
        did_work = dict(group_result(1, "a"), children=[
            {"index": 0, "op": "tapIfPresent", "ok": True, "skipped": True},
            {"index": 1, "op": "tap", "ok": True},
        ])
        reply = {"ok": True, "results": [
            shot_result(0, "/tmp/one.png"), did_work, shot_result(2, "/tmp/two.png"),
        ]}
        self.assertEqual(len(report.shots_from(reply, captions)), 2)

    def test_a_skip_does_not_fold_onto_a_screen_that_is_not_adjacent(self):
        """A screenshot that succeeds but yields no path is dropped, so the previous kept shot is no
        longer the screen before this record — folding onto it would caption the wrong image."""
        reply = {"results": [
            shot_result(0, "/tmp/before.png"),
            group_result(1, "a"),
            {"index": 2, "op": "screenshot", "ms": 1, "ok": True, "info": ""},
            group_result(3, "b", skipped=True),
            shot_result(4, "/tmp/after-b.png"),
        ]}
        captions = {0: "before", 2: "after a", 4: "after b"}
        shots = report.shots_from(reply, captions)
        self.assertEqual([shot["caption"] for shot in shots], ["before", "after b"])
        self.assertNotIn("also", shots[0])

    def test_a_childless_group_is_not_vacuously_folded(self):
        _, _, captions = report.plan([group("a")], ["a"])
        reply = {"ok": True, "results": [
            shot_result(0, "/tmp/one.png"), group_result(1, "a"), shot_result(2, "/tmp/two.png"),
        ]}
        self.assertEqual(len(report.shots_from(reply, captions)), 2)

    def test_an_empty_reply_yields_no_shots(self):
        self.assertEqual(report.shots_from(None, {}), [])
        self.assertEqual(report.shots_from({"ok": True, "results": []}, {0: "before"}), [])

    def test_a_failed_screenshot_step_contributes_nothing(self):
        _, _, captions = report.plan([], [])
        reply = {"ok": False, "results": [{"index": 0, "op": "screenshot", "ok": False}]}
        self.assertEqual(report.shots_from(reply, captions), [])


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def shot(self, caption, body):
        path = os.path.join(self.directory, "%s.png" % caption.replace(" ", "-"))
        with open(path, "wb") as handle:
            handle.write(body)
        return {"caption": caption, "path": path}

    def test_a_run_of_identical_screens_collapses_into_the_first(self):
        shots = report.dedupe([
            self.shot("before", b"A"),
            self.shot("after a", b"A"),
            self.shot("after b", b"A"),
            self.shot("after c", b"B"),
        ])
        self.assertEqual([entry["caption"] for entry in shots], ["before", "after c"])
        self.assertEqual(shots[0]["also"], ["after a", "after b"])
        self.assertNotIn("also", shots[1])

    def test_folding_preserves_captions_already_folded_upstream(self):
        shots = report.dedupe([
            dict(self.shot("before", b"A"), also=["after a"]),
            dict(self.shot("after b", b"A"), also=["after c"]),
        ])
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["also"], ["after a", "after b", "after c"])

    def test_dedupe_leaves_its_argument_untouched_and_repeats_itself(self):
        source = [
            dict(self.shot("before", b"A"), also=["after a"]),
            dict(self.shot("after b", b"A"), also=["after c"]),
        ]
        first = report.dedupe(source)
        self.assertEqual(source[0]["also"], ["after a"])
        self.assertEqual(report.dedupe(source)[0]["also"], first[0]["also"])

    def test_a_shot_marked_keep_is_never_folded_away(self):
        shots = report.dedupe([
            self.shot("before", b"A"),
            dict(self.shot("FAILURE", b"A"), keep=True),
        ])
        self.assertEqual([entry["caption"] for entry in shots], ["before", "FAILURE"])

    def test_shots_from_output_folds_further_on_identical_bytes(self):
        _, _, captions = report.plan([group("a"), group("b")], ["a", "b"])
        paths = [self.shot(name, b"A")["path"] for name in ("one", "two", "three")]
        reply = {"ok": True, "results": [
            shot_result(0, paths[0]),
            group_result(1, "a", skipped=True),
            shot_result(2, paths[1]),
            group_result(3, "b"),
            shot_result(4, paths[2]),
        ]}
        shots = report.dedupe(report.shots_from(reply, captions))
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["also"], ["after a", "after b"])

    def test_a_screen_that_returns_later_is_kept(self):
        shots = report.dedupe([
            self.shot("before", b"A"), self.shot("after a", b"B"), self.shot("after b", b"A"),
        ])
        self.assertEqual([entry["caption"] for entry in shots], ["before", "after a", "after b"])

    def test_unreadable_shots_are_never_folded_together(self):
        shots = report.dedupe([
            {"caption": "before", "path": "/nope/one.png"},
            {"caption": "after a", "path": "/nope/two.png"},
        ])
        self.assertEqual(len(shots), 2)

    def test_folded_captions_are_rendered_on_the_surviving_figure(self):
        html = report.render({
            "app": "a", "names": [], "ok": True, "wallMs": 1, "udid": None,
            "startedAt": "x", "records": [], "error": None,
            "shots": [dict(self.shot("before", PNG), also=["after a", "after b"])],
        })
        self.assertIn("unchanged after a, after b", html)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.resolved = [
            {"name": "a", "description": "adds Korean", "verifiesWhy": "the key appears"},
            {"name": "b", "description": "", "verifiesWhy": ""},
        ]
        self.groups = [group("a"), group("b")]

    def build(self, reply, shots=()):
        _, positions, _ = report.plan(self.groups, ["a", "b"])
        return report.build_context(
            app="io.dtails.Slyder",
            names=["a", "b"],
            resolved=self.resolved,
            reply=reply,
            positions=positions,
            wall_ms=4200,
            shots=list(shots),
            sim=SIM,
        )

    def test_passing_run_reports_every_record_status(self):
        context = self.build({"ok": True, "results": [
            shot_result(0, "/tmp/a.png"),
            group_result(1, "a", skipped=True),
            shot_result(2, "/tmp/b.png"),
            group_result(3, "b", ms=1500),
            shot_result(4, "/tmp/c.png"),
        ]})
        self.assertTrue(context["ok"])
        self.assertEqual([entry["status"] for entry in context["records"]], ["skip", "ok"])
        self.assertEqual(context["records"][1]["ms"], 1500)
        self.assertEqual(context["records"][0]["verifiesWhy"], "the key appears")
        self.assertEqual(context["wallMs"], 4200)
        self.assertEqual(context["app"], "io.dtails.Slyder")
        self.assertIsNone(context["error"])

    def test_a_record_the_aborted_batch_never_reached_is_marked_not_run(self):
        context = self.build({
            "ok": False,
            "results": [shot_result(0, "/tmp/a.png"), group_result(1, "a", ok=False)],
            "error": {"reason": "notFound", "detail": "button 'Add'", "failedPath": [1, 2],
                      "snapshot": ["{\"type\":\"button\"}"]},
        })
        self.assertFalse(context["ok"])
        self.assertEqual([entry["status"] for entry in context["records"]], ["FAIL", "not run"])
        self.assertEqual(context["error"]["reason"], "notFound")
        self.assertEqual(context["error"]["record"], "a")
        self.assertEqual(context["error"]["path"], "1.2")

    def test_failure_outside_any_record_leaves_the_record_name_empty(self):
        context = self.build({
            "ok": False,
            "results": [{"index": 0, "op": "screenshot", "ok": False}],
            "error": {"reason": "badStep", "detail": "boom", "failedPath": [0], "snapshot": []},
        })
        self.assertIsNone(context["error"]["record"])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.shot = os.path.join(self.directory, "shot-1.png")
        with open(self.shot, "wb") as handle:
            handle.write(PNG)

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def context(self, **overrides):
        base = {
            "app": "io.dtails.Slyder",
            "names": ["add-korean"],
            "ok": True,
            "wallMs": 4200,
            "sim": SIM,
            "startedAt": "2026-09-02T10:00:00+00:00",
            "records": [{"name": "add-korean", "status": "ok", "ms": 900,
                         "description": "adds the Korean keyboard", "verifiesWhy": "the key appears"}],
            "shots": [{"caption": "before", "path": self.shot}],
            "error": None,
        }
        base.update(overrides)
        return base

    def test_a_passing_run_renders_its_verdict_records_and_inlined_shot(self):
        html = report.render(self.context())
        self.assertIn("PASS", html)
        self.assertIn("add-korean", html)
        self.assertIn("io.dtails.Slyder", html)
        self.assertIn("4.2", html)
        self.assertIn("data:image/png;base64,%s" % base64.b64encode(PNG).decode(), html)
        self.assertNotIn("<img src=\"http", html)

    def test_a_failing_run_renders_the_error_and_its_snapshot(self):
        html = report.render(self.context(
            ok=False,
            records=[{"name": "add-korean", "status": "FAIL", "ms": 900,
                      "description": "", "verifiesWhy": ""}],
            error={"reason": "notFound", "detail": "button 'Add'", "path": "1.2",
                   "record": "add-korean", "snapshot": ["{\"type\":\"button\"}"]},
        ))
        self.assertIn("FAIL", html)
        self.assertIn("notFound", html)
        self.assertIn("button &#x27;Add&#x27;", html)
        self.assertIn("<details", html)

    def test_untrusted_text_is_escaped(self):
        html = report.render(self.context(
            records=[{"name": "x", "status": "ok", "ms": 1,
                      "description": "<script>alert(1)</script>", "verifiesWhy": ""}],
        ))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_an_unreadable_shot_is_noted_rather_than_crashing(self):
        html = report.render(self.context(shots=[{"caption": "before", "path": "/nope/gone.png"}]))
        self.assertIn("unavailable", html)

    def test_an_unreadable_shot_still_names_the_records_folded_into_it(self):
        html = report.render(self.context(shots=[
            {"caption": "before", "path": "/nope/gone.png", "also": ["after a", "after b"]},
        ]))
        self.assertIn("unavailable", html)
        self.assertIn("unchanged after a, after b", html)

    def test_a_large_shot_is_downscaled_before_inlining(self):
        big = os.path.join(self.directory, "big.png")
        import subprocess
        subprocess.run(["sips", "-s", "format", "png", "-z", "2400", "1200", self.shot, "--out", big],
                       capture_output=True)
        if not os.path.exists(big):
            self.skipTest("sips unavailable")
        html = report.render(self.context(shots=[{"caption": "before", "path": big}]))
        encoded = html.split("base64,")[1].split('"')[0]
        self.assertLess(len(base64.b64decode(encoded)), os.path.getsize(big))

    def test_a_run_with_no_shots_still_renders(self):
        self.assertIn("PASS", report.render(self.context(shots=[])))

    def test_a_localized_run_names_its_language_in_the_meta_line(self):
        html = report.render(self.context(locale={"lang": "ko-US", "untranslated": [], "labels": 14}))
        self.assertIn("ko-US", html)

    def test_untranslated_labels_are_counted_in_the_meta_line(self):
        html = report.render(self.context(
            locale={"lang": "ko-US", "untranslated": ["Allow", "Done"], "labels": 14}))
        self.assertIn("2 of 14 labels untranslated", html)

    def test_an_english_run_says_nothing_about_locale(self):
        self.assertNotIn("untranslated", report.render(self.context()))


class BuildContextLocaleTests(unittest.TestCase):
    def build(self, **kwargs):
        return report.build_context(
            app="io.dtails.Slyder", names=["add-korean"],
            resolved=[{"name": "add-korean", "description": "", "verifiesWhy": ""}],
            reply={"ok": True, "results": [{"status": "ok", "ms": 5}]}, positions=[0],
            wall_ms=10, shots=[], **kwargs)

    def test_locale_round_trips_into_the_context(self):
        locale = {"lang": "ko-US", "untranslated": ["Allow"], "labels": 14}
        self.assertEqual(self.build(locale=locale)["locale"], locale)

    def test_no_locale_is_none(self):
        self.assertIsNone(self.build()["locale"])


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def context(self):
        return {
            "app": "io.dtails.Slyder", "names": ["a"], "ok": True, "wallMs": 10, "sim": None,
            "startedAt": "2026-09-02T10:00:00+00:00",
            "records": [{"name": "a", "status": "ok", "ms": 1, "description": "", "verifiesWhy": ""}],
            "shots": [], "error": None,
        }

    def test_writes_a_self_contained_report_and_returns_its_absolute_path(self):
        directory = os.path.join(self.root, "run-1")
        path = report.write(self.context(), directory)
        self.assertEqual(path, os.path.join(directory, "report.html"))
        self.assertTrue(os.path.isabs(path))
        with open(path) as handle:
            self.assertIn("PASS", handle.read())

    def test_saves_a_full_resolution_copy_of_each_shot_and_links_it(self):
        directory = os.path.join(self.root, "run-3")
        source = os.path.join(self.root, "src.png")
        with open(source, "wb") as handle:
            handle.write(PNG)
        context = dict(self.context(), shots=[{"caption": "after add-korean", "path": source}])
        with open(report.write(context, directory)) as handle:
            body = handle.read()
        self.assertIn('href="01-after-add-korean.png"', body)
        self.assertTrue(os.path.exists(os.path.join(directory, "01-after-add-korean.png")))

    def test_a_shot_whose_source_vanished_is_still_rendered_without_a_link(self):
        directory = os.path.join(self.root, "run-4")
        context = dict(self.context(), shots=[{"caption": "before", "path": "/nope/gone.png"}])
        with open(report.write(context, directory)) as handle:
            body = handle.read()
        self.assertIn("unavailable", body)
        self.assertNotIn("href=", body)

    def test_writes_a_machine_readable_sidecar(self):
        directory = os.path.join(self.root, "run-2")
        report.write(self.context(), directory)
        with open(os.path.join(directory, "run.json")) as handle:
            self.assertEqual(json.load(handle)["app"], "io.dtails.Slyder")

    def test_default_dir_is_named_for_the_time_and_app_under_the_root(self):
        directory = report.default_dir("io.dtails.Slyder", root=self.root)
        self.assertTrue(directory.startswith(os.path.join(self.root, "")))
        self.assertTrue(os.path.basename(directory).endswith("-io.dtails.Slyder"))

    def test_write_folds_identical_screens_into_one_figure(self):
        """write() is dedupe's only production call site. Without this, disconnecting it there
        leaves the whole suite green."""
        first = os.path.join(self.root, "one.png")
        second = os.path.join(self.root, "two.png")
        for path in (first, second):
            with open(path, "wb") as handle:
                handle.write(PNG)
        context = dict(self.context(), shots=[
            {"caption": "before", "path": first},
            {"caption": "after a", "path": second},
        ])
        with open(report.write(context, os.path.join(self.root, "run-fold"))) as handle:
            html = handle.read()
        self.assertEqual(html.count("<figure"), 1)
        self.assertIn("after a", html)

    def test_write_keeps_a_shot_marked_keep_even_when_identical(self):
        """The FAILURE screen is byte-identical to the one before it whenever the failure left the
        screen untouched, which is the common case and the image the reader actually needs."""
        first = os.path.join(self.root, "one.png")
        second = os.path.join(self.root, "two.png")
        for path in (first, second):
            with open(path, "wb") as handle:
                handle.write(PNG)
        context = dict(self.context(), shots=[
            {"caption": "before", "path": first},
            {"caption": "FAILURE", "path": second, "keep": True},
        ])
        with open(report.write(context, os.path.join(self.root, "run-keep"))) as handle:
            html = handle.read()
        self.assertEqual(html.count("<figure"), 2)

    def test_back_to_back_runs_get_distinct_directories(self):
        first = report.default_dir("com.example.Demo", root=self.root)
        second = report.default_dir("com.example.Demo", root=self.root)
        self.assertNotEqual(first, second)

    def test_started_at_is_taken_from_the_caller_when_given(self):
        context = report.build_context(
            app="a", names=[], resolved=[], reply={"ok": True}, positions=[], wall_ms=1,
            shots=[], started_at="2020-01-01T00:00:00+00:00",
        )
        self.assertEqual(context["startedAt"], "2020-01-01T00:00:00+00:00")

    def test_default_dir_sanitises_a_hostile_app_id(self):
        directory = report.default_dir("../../etc/passwd", root=self.root)
        self.assertEqual(os.path.dirname(directory), self.root)
        self.assertNotIn("..", os.path.basename(directory))


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make(self, name, stamp):
        directory = os.path.join(self.root, name)
        os.makedirs(directory)
        os.utime(directory, (stamp, stamp))
        return directory

    def test_keeps_the_newest_runs_and_removes_the_rest(self):
        for index in range(5):
            self.make("run-%d" % index, 1000 + index)
        report.prune(self.root, keep=2)
        self.assertEqual(sorted(os.listdir(self.root)), ["run-3", "run-4"])

    def test_leaves_loose_files_alone(self):
        with open(os.path.join(self.root, "note.txt"), "w") as handle:
            handle.write("x")
        self.make("run-0", 1000)
        report.prune(self.root, keep=0)
        self.assertEqual(os.listdir(self.root), ["note.txt"])

    def test_a_directory_that_vanishes_mid_prune_is_not_an_error(self):
        kept = self.make("run-keep", 2000)
        gone = self.make("run-gone", 1000)
        real_stat = os.stat

        def flaky(path, *args, **kwargs):
            if path == gone:
                shutil.rmtree(gone, ignore_errors=True)
                raise FileNotFoundError(gone)
            return real_stat(path, *args, **kwargs)

        os.stat = flaky
        try:
            report.prune(self.root, keep=1)
        finally:
            os.stat = real_stat
        self.assertTrue(os.path.isdir(kept))

    def test_the_sort_key_survives_a_run_that_is_already_gone(self):
        """Reaches _mtime's own except. The test above never does: it raises inside os.path.isdir,
        which swallows the error and drops the entry before the sort key is ever applied."""
        self.assertEqual(report._mtime(os.path.join(self.root, "never-existed")), 0.0)

    def test_prune_orders_by_mtime_even_when_one_entry_is_unstatable(self):
        newest = self.make("run-new", 3000)
        self.make("run-old", 1000)
        real_stat = report.os.stat

        def flaky(path, *args, **kwargs):
            if path.endswith("run-old"):
                raise PermissionError(path)
            return real_stat(path, *args, **kwargs)

        report.os.stat = flaky
        try:
            report.prune(self.root, keep=1)
        finally:
            report.os.stat = real_stat
        self.assertTrue(os.path.isdir(newest))

    def test_a_missing_root_is_not_an_error(self):
        report.prune(os.path.join(self.root, "nope"), keep=1)


if __name__ == "__main__":
    unittest.main()
