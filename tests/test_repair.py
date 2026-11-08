import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import repair, timing


def element(**kwargs):
    base = {"app": "com.example.Demo", "type": "button", "id": None, "label": None, "frame": [0, 0, 10, 10]}
    base.update(kwargs)
    return base


class ParseSnapshotTests(unittest.TestCase):
    def test_parses_json_lines(self):
        lines = [json.dumps(element(id="a")), "not json", json.dumps(element(id="b"))]
        parsed = repair.parse_snapshot(lines)
        self.assertEqual([e["id"] for e in parsed], ["a", "b"])

    def test_a_quote_in_a_label_survives(self):
        lines = [json.dumps(element(label='say "hi"'))]
        self.assertEqual(repair.parse_snapshot(lines)[0]["label"], 'say "hi"')

    def test_accepts_a_single_newline_joined_string(self):
        lines = "\n".join([json.dumps(element(id="a")), json.dumps(element(id="b"))])
        self.assertEqual(len(repair.parse_snapshot(lines)), 2)


class CandidateTests(unittest.TestCase):
    def test_prefers_the_remembered_label_when_an_id_rotted(self):
        selector = {"app": "com.example.Demo", "type": "button", "id": "settings.row.old", "label": "Languages"}
        elements = [
            element(id="settings.row.languages", label="Languages"),
            element(id="settings.row.themes", label="Themes"),
        ]
        best = repair.candidates(selector, elements)[0]
        self.assertEqual(best["selector"]["id"], "settings.row.languages")
        self.assertEqual(best["basis"], "id")

    def test_fuzzy_id_match_above_the_threshold(self):
        selector = {"app": "com.example.Demo", "type": "button", "id": "language.picker.add"}
        elements = [element(id="language.picker.addNew", label="Add")]
        best = repair.candidates(selector, elements)[0]
        self.assertEqual(best["selector"]["id"], "language.picker.addNew")
        self.assertGreater(best["score"], 0.6)

    def test_nothing_close_returns_nothing(self):
        selector = {"app": "com.example.Demo", "type": "button", "id": "language.picker.add"}
        elements = [element(id="zzz.unrelated.control", label="Wholly different")]
        self.assertEqual(repair.candidates(selector, elements), [])

    def test_a_truncated_label_may_only_become_label_contains(self):
        selector = {"app": "com.example.Demo", "type": "staticText", "label": "The quick brown fox jumps"}
        elements = [element(type="staticText", id=None, label="The quick brown fox jum", truncated=True)]
        best = repair.candidates(selector, elements)[0]
        self.assertNotIn("label", best["selector"])
        self.assertEqual(best["selector"]["labelContains"], "The quick brown fox jum")
        self.assertEqual(best["basis"], "label")

    def test_an_element_with_no_id_and_no_label_is_not_a_candidate(self):
        selector = {"app": "com.example.Demo", "type": "button", "id": "key.q"}
        self.assertEqual(repair.candidates(selector, [element(id=None, label=None)]), [])

    def test_candidates_are_capped_and_sorted(self):
        selector = {"app": "com.example.Demo", "type": "button", "id": "key.q"}
        elements = [element(id="key.q%d" % i, label="q%d" % i) for i in range(20)]
        found = repair.candidates(selector, elements, limit=3)
        self.assertEqual(len(found), 3)
        self.assertEqual(found, sorted(found, key=lambda c: -c["score"]))

    def test_an_id_candidate_outranks_a_label_candidate_of_equal_score(self):
        selector = {"app": "com.example.Demo", "type": "button", "label": "Go"}
        elements = [element(id=None, label="Go"), element(id="go.button", label="Go")]
        self.assertEqual(repair.candidates(selector, elements)[0]["basis"], "id")

    def test_a_different_app_is_never_proposed(self):
        selector = {"app": "com.example.Demo", "type": "button", "id": "key.q"}
        self.assertEqual(repair.candidates(selector, [element(app="com.other", id="key.q")]), [])


class PatchTests(unittest.TestCase):
    def make(self):
        return {
            "app": "com.example.Demo",
            "name": "x",
            "steps": [
                {"op": "tap", "sel": {"label": "a"}},
                {"op": "group", "steps": [{"op": "tap", "sel": {"id": "old.id"}}]},
            ],
            "history": [],
            "timing": {},
            "health": {"runs": 3, "failures": 1, "lastOk": None, "repairedAt": None},
        }

    def test_replaces_the_selector_at_a_nested_path(self):
        record = self.make()
        patched = repair.patch(record, [1, 0], {"id": "new.id"})
        self.assertEqual(patched["steps"][1]["steps"][0]["sel"], {"id": "new.id"})
        self.assertEqual(record["steps"][1]["steps"][0]["sel"], {"id": "old.id"})

    def test_stamps_repaired_at_and_appends_history(self):
        patched = repair.patch(self.make(), [0], {"id": "new.id"})
        self.assertTrue(patched["health"]["repairedAt"])
        self.assertEqual(len(patched["history"]), 1)
        self.assertIn("new.id", patched["history"][0])

    def test_drops_that_steps_stale_timing_samples(self):
        record = self.make()
        step = record["steps"][1]["steps"][0]
        record["timing"][timing.entry_key([1, 0], step)] = [10, 20, 30]
        record["timing"][timing.entry_key([0], record["steps"][0])] = [1, 2, 3]
        patched = repair.patch(record, [1, 0], {"id": "new.id"})
        self.assertEqual([key for key in patched["timing"] if key.startswith("1.0|")], [])
        self.assertEqual(len(patched["timing"]), 1)

    def test_a_bad_path_raises(self):
        with self.assertRaises(repair.RepairError):
            repair.patch(self.make(), [9], {"id": "new.id"})
        with self.assertRaises(repair.RepairError):
            repair.patch(self.make(), [], {"id": "new.id"})


class DoRepairTests(unittest.TestCase):
    def record(self):
        return {
            "app": "com.example.Demo",
            "name": "x",
            "steps": [{"op": "tap", "sel": {"app": "com.example.Demo", "type": "button", "id": "old.id", "label": "Go"}}],
            "history": [],
            "timing": {},
            "health": {},
        }

    def reply(self):
        return {
            "seq": 1,
            "ok": False,
            "results": [],
            "elapsedMs": 10,
            "error": {
                "reason": "notFound",
                "detail": "no match for old.id",
                "failedPath": [0, 0],
                "snapshot": [json.dumps(element(id="new.id", label="Go"))],
            },
        }

    def test_probes_every_candidate_in_one_batch_with_an_explicit_budget(self):
        from uiflow.commands import do

        sent = []

        def probe(steps, **kwargs):
            sent.append(steps)
            return {"ok": True, "results": [{"index": 0, "op": "firstMatch", "ms": 20, "ok": True, "info": "index=0"}]}

        saved = []
        patched = do.repair_once(
            [self.record()], self.reply(), probe=probe, save=saved.append, auto_repair=True, echo=lambda line: None
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0]["op"], "firstMatch")
        self.assertEqual(sent[0][0]["timeoutMs"], do.PROBE_TIMEOUT_MS)
        self.assertTrue(all(s.get("timeoutMs") == do.PROBE_TIMEOUT_MS for s in [sent[0][0]]))
        self.assertTrue(patched)
        self.assertEqual(saved[0]["steps"][0]["sel"]["id"], "new.id")

    def test_a_label_only_repair_needs_confirmation(self):
        from uiflow.commands import do

        record = self.record()
        record["steps"][0]["sel"] = {"app": "com.example.Demo", "type": "button", "label": "Go"}
        reply = self.reply()
        reply["error"]["snapshot"] = [json.dumps(element(id=None, label="Go"))]

        def probe(steps, **kwargs):
            return {"ok": True, "results": [{"index": 0, "op": "firstMatch", "ms": 20, "ok": True, "info": "index=0"}]}

        saved = []
        patched = do.repair_once(
            [record], reply, probe=probe, save=saved.append, auto_repair=False,
            confirm=lambda candidate: False, echo=lambda line: None,
        )
        self.assertFalse(patched)
        self.assertEqual(saved, [])

    def test_no_candidates_means_no_repair(self):
        from uiflow.commands import do

        reply = self.reply()
        reply["error"]["snapshot"] = [json.dumps(element(id="zzz.nothing.alike", label="Unrelated"))]
        saved = []
        patched = do.repair_once(
            [self.record()], reply, probe=lambda *a, **k: self.fail("must not probe"),
            save=saved.append, auto_repair=True, echo=lambda line: None,
        )
        self.assertFalse(patched)

    def test_a_localized_run_with_no_oracle_persists_nothing(self):
        """`--no-localize` also turned the PERSISTENCE guard off: two concerns, one switch."""
        from uiflow.commands import do

        record = self.record()
        record["steps"][0]["sel"] = {"app": "com.example.Demo", "type": "button",
                                     "label": "Notifications"}
        reply = self.reply()
        reply["error"]["snapshot"] = [json.dumps(element(id=None, label="Notificaciones"))]

        def probe(steps, **kwargs):
            return {"ok": True, "results": [{"index": 0, "op": "firstMatch", "ms": 20,
                                             "ok": True, "info": "index=0"}]}

        saved = []
        lines = []
        self.assertIsNone(do.repair_once(
            [record], reply, probe=probe, save=saved.append, auto_repair=True,
            echo=lines.append, english_only=True))
        self.assertEqual(saved, [])
        self.assertTrue([line for line in lines if "not persisted" in line], lines)
        self.assertTrue(do.repair_once(
            [record], reply, probe=probe, save=saved.append, auto_repair=True,
            echo=lines.append, english_only=False))
        self.assertEqual(saved[0]["steps"][0]["sel"]["label"], "Notificaciones")

    def test_a_non_selector_failure_is_not_repaired(self):
        from uiflow.commands import do

        reply = self.reply()
        reply["error"]["reason"] = "unknownOp"
        patched = do.repair_once(
            [self.record()], reply, probe=lambda *a, **k: self.fail("must not probe"),
            save=lambda r: None, auto_repair=True, echo=lambda line: None,
        )
        self.assertFalse(patched)


class RepairThroughAFragmentTests(unittest.TestCase):
    def setUp(self):
        import json as json_
        import shutil
        import tempfile

        from uiflow import records

        self.dir = tempfile.mkdtemp(prefix="uiflow-repair-frag-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        self.app = "com.example.Demo"
        for name, steps, extra in (
            ("frag", [{"op": "tap", "sel": {"app": "com.example.Demo", "type": "button", "label": "Genral"}}],
             {"kind": "fragment"}),
            ("caller", [{"op": "use", "ref": "frag"}], {}),
        ):
            payload = {"app": self.app, "name": name, "steps": steps, "timing": {},
                       "health": {"runs": 0, "failures": 0, "lastOk": None, "repairedAt": None}}
            payload.update(extra)
            directory = os.path.join(self.dir, self.app)
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, name + ".json"), "w") as handle:
                json_.dump(payload, handle)
        records.invalidate()

    def test_a_failure_inside_a_fragment_patches_the_fragment(self):
        from uiflow import records
        from uiflow.commands import do

        resolved = [records.load(self.app, "caller")]
        _, positions, _, origins = do.build_batch(resolved, capture=False)
        reply = {
            "ok": False,
            "error": {
                "reason": "notFound",
                "failedPath": [0, 0, 0],
                "snapshot": [json.dumps({"app": "com.example.Demo", "type": "button",
                                         "id": "com.apple.settings.general", "label": "General"})],
            },
        }
        saved = []
        patched = do.repair_once(
            resolved, reply,
            probe=lambda steps, **kwargs: {"ok": True, "results": [{"info": "index=0"}]},
            save=saved.append, auto_repair=True, echo=lambda line: None,
            positions=positions, origins=origins,
        )
        self.assertIsNotNone(patched)
        self.assertEqual(patched["name"], "frag")
        self.assertEqual(saved[0]["steps"][0]["sel"]["id"], "com.apple.settings.general")


class LocalizedRepairTests(unittest.TestCase):
    class FakeOracle:
        REVERSE = {"일반": ["General"], "확인": ["Acknowledge", "OK"]}

        def originals(self, label):
            return list(self.REVERSE.get(label) or [])

        def canonical(self, label):
            found = self.originals(label)
            return found[0] if len(found) == 1 else None

        def knows_english(self, label):
            return label in ("General", "Keyboard")

        def knows_localized(self, label):
            return label in self.REVERSE

    def test_scoring_uses_the_selector_that_was_actually_sent(self):
        """A ko snapshot against an English selector scores 0.0 and finds nothing."""
        record_selector = {"app": "com.apple.Preferences", "type": "button", "label": "General"}
        sent = {"app": "com.apple.Preferences", "type": "button",
                "labelAnyOf": ["General", "일반"]}
        elements = [element(app="com.apple.Preferences", id="com.apple.settings.general", label="일반")]
        self.assertEqual(repair.candidates(record_selector, elements), [])
        found = repair.candidates(record_selector, elements, sent=sent)
        self.assertEqual(found[0]["selector"]["id"], "com.apple.settings.general")

    def test_a_localized_label_candidate_is_canonicalized_before_it_is_persisted(self):
        proposed = {"app": "com.apple.Preferences", "type": "button", "label": "일반"}
        self.assertEqual(
            repair.canonical_selector(proposed, self.FakeOracle())["label"], "General")

    def test_an_ambiguous_candidate_refuses_to_be_persisted(self):
        proposed = {"app": "com.apple.Preferences", "type": "button", "label": "확인"}
        self.assertIsNone(repair.canonical_selector(proposed, self.FakeOracle()))

    def test_a_carried_english_fragment_is_no_longer_thrown_away(self):
        """`iter_pairs` drops identity pairs, so `Bluetooth` was never a `pair.en` and died here."""
        proposed = {"type": "cell", "id": "com.apple.settings.general",
                    "descendant": {"labelContains": "Keyboards,"}}
        self.assertEqual(repair.canonical_selector(proposed, self.FakeOracle()), proposed)
        bluetooth = {"type": "button", "label": "Bluetooth"}
        self.assertEqual(repair.canonical_selector(bluetooth, self.FakeOracle()), bluetooth)

    def test_without_an_oracle_the_candidate_passes_through(self):
        proposed = {"type": "button", "label": "General"}
        self.assertEqual(repair.canonical_selector(proposed, None), proposed)

    def test_on_a_localized_simulator_no_oracle_means_nothing_is_persisted(self):
        """`--no-localize` and a failed oracle build turned the guard off with it."""
        proposed = {"type": "button", "label": "Notificaciones"}
        self.assertIsNone(repair.canonical_selector(proposed, None, required=True))
        self.assertEqual(
            repair.canonical_selector(proposed, self.FakeOracle(), required=True), proposed)


if __name__ == "__main__":
    unittest.main()
