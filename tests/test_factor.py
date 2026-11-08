import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import factor, records, sources, timing
from uiflow.commands import factor as factor_command

APP = "com.example.Demo"
SETTINGS = "com.apple.Preferences"
WALK = [
    {"op": "launch", "app": SETTINGS},
    {"op": "tap", "sel": {"app": SETTINGS, "type": "button", "label": "General"}},
    {"op": "tap", "sel": {"app": SETTINGS, "type": "button", "label": "Keyboard"}},
]


class FactorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-factor-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()

    def write(self, app, name, steps, **extra):
        payload = {"app": app, "name": name, "steps": steps}
        payload.update(extra)
        directory = os.path.join(self.dir, app)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump(payload, handle)
        records.invalidate()
        return payload

    def alphabet(self, count):
        return [{"op": "tap", "sel": {"app": APP, "type": "button", "label": chr(97 + n)}}
                for n in range(count)]

    def two_callers(self, **extra):
        self.write(APP, "one", WALK + [{"op": "tap", "sel": {"label": "z"}}], **extra)
        self.write(APP, "two", WALK + [{"op": "tap", "sel": {"label": "q"}}])

    def test_proposals_find_the_shared_walk(self):
        self.two_callers()
        found = factor.proposals()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["length"], 3)
        self.assertEqual(found[0]["steps"], WALK)

    def test_the_fragment_belongs_to_the_app_its_steps_drive(self):
        self.two_callers()
        self.assertEqual(factor.fragment_app(WALK, APP), SETTINGS)

    def test_fragment_app_falls_back_when_the_run_is_not_unanimous(self):
        mixed = WALK[:2] + [{"op": "tap", "sel": {"app": APP, "label": "z"}}]
        self.assertEqual(factor.fragment_app(mixed, APP), APP)

    def test_apply_writes_a_fragment_and_rewrites_every_caller(self):
        self.two_callers()
        outcome = factor.apply(factor.proposals()[0])
        self.assertTrue(os.path.exists(outcome["fragment"]))
        self.assertEqual(len(outcome["rewrote"]), 2)
        fragment = records.load_ref(outcome["ref"])
        self.assertEqual(fragment["kind"], records.FRAGMENT)
        self.assertEqual(fragment["app"], SETTINGS)
        self.assertEqual(fragment["steps"], WALK)
        caller = records.load(APP, "one")
        self.assertEqual(caller["steps"][0], {"op": "use", "ref": outcome["ref"]})
        self.assertEqual(caller["steps"][1], {"op": "tap", "sel": {"label": "z"}})

    def test_apply_appends_history_to_everything_it_touched(self):
        self.two_callers()
        outcome = factor.apply(factor.proposals()[0])
        self.assertTrue(any("factor" in line for line in records.load(APP, "one")["history"]))
        self.assertTrue(any("factor" in line for line in records.load_ref(outcome["ref"])["history"]))

    def test_timing_samples_migrate_into_the_fragment(self):
        table = {
            timing.entry_key([0], WALK[0]): [10, 11, 12],
            timing.entry_key([1], WALK[1]): [20],
            timing.entry_key([3], {"op": "tap", "sel": {"label": "z"}}): [99],
        }
        self.two_callers(timing=table)
        outcome = factor.apply(factor.proposals()[0])
        fragment = records.load_ref(outcome["ref"])
        self.assertEqual(fragment["timing"][timing.entry_key([0], WALK[0])], {"hits": [10, 11, 12]})
        self.assertEqual(fragment["timing"][timing.entry_key([1], WALK[1])], {"hits": [20]})

    def test_the_callers_trailing_timing_keys_shift_to_their_new_positions(self):
        tail = {"op": "tap", "sel": {"label": "z"}}
        self.two_callers(timing={timing.entry_key([3], tail): [99]})
        factor.apply(factor.proposals()[0])
        caller = records.load(APP, "one")
        self.assertEqual(caller["timing"][timing.entry_key([1], tail)], [99])
        self.assertNotIn(timing.entry_key([3], tail), caller["timing"])

    def test_samples_from_two_callers_merge_into_one_fragment_series(self):
        self.write(APP, "one", WALK + [{"op": "tap", "sel": {"label": "z"}}],
                   timing={timing.entry_key([0], WALK[0]): [10]})
        self.write(APP, "two", WALK + [{"op": "tap", "sel": {"label": "q"}}],
                   timing={timing.entry_key([0], WALK[0]): [30]})
        outcome = factor.apply(factor.proposals()[0])
        entry = records.load_ref(outcome["ref"])["timing"][timing.entry_key([0], WALK[0])]
        self.assertEqual(sorted(entry["hits"]), [10, 30])

    def test_rekey_timing_moves_a_dict_entry_without_flattening_it_to_its_keys(self):
        key = timing.entry_key([1], WALK[1])
        _, fragment = factor.rekey_timing({key: {"hits": [10, 11], "misses": 3}}, 0, 3)
        self.assertEqual(fragment[key], {"hits": [10, 11], "misses": 3})

    def test_rekey_timing_does_not_alias_the_entry_it_moved(self):
        key = timing.entry_key([1], WALK[1])
        original = {"hits": [10], "misses": 3}
        _, fragment = factor.rekey_timing({key: original}, 0, 3)
        fragment[key]["hits"].append(99)
        fragment[key]["misses"] = 0
        self.assertEqual(original, {"hits": [10], "misses": 3})

    def test_rekey_timing_still_moves_a_legacy_list_entry(self):
        key = timing.entry_key([1], WALK[1])
        _, fragment = factor.rekey_timing({key: [10, 11]}, 0, 3)
        self.assertEqual(fragment[key], [10, 11])

    def test_two_callers_on_one_key_combine_hits_and_keep_the_smaller_miss_count(self):
        key = timing.entry_key([0], WALK[0])
        self.write(APP, "one", WALK + [{"op": "tap", "sel": {"label": "z"}}],
                   timing={key: {"hits": [10], "misses": 7}})
        self.write(APP, "two", WALK + [{"op": "tap", "sel": {"label": "q"}}],
                   timing={key: {"hits": [30], "misses": 2}})
        outcome = factor.apply(factor.proposals()[0])
        entry = records.load_ref(outcome["ref"])["timing"][key]
        self.assertEqual(sorted(entry["hits"]), [10, 30])
        self.assertEqual(entry["misses"], 2)

    def test_a_legacy_list_entry_merges_with_a_dict_entry(self):
        key = timing.entry_key([0], WALK[0])
        self.write(APP, "one", WALK + [{"op": "tap", "sel": {"label": "z"}}],
                   timing={key: [10]})
        self.write(APP, "two", WALK + [{"op": "tap", "sel": {"label": "q"}}],
                   timing={key: {"hits": [30], "misses": 5}})
        outcome = factor.apply(factor.proposals()[0])
        entry = records.load_ref(outcome["ref"])["timing"][key]
        self.assertEqual(sorted(entry["hits"]), [10, 30])
        self.assertEqual(entry.get("misses", 0), 0)

    def test_a_run_already_behind_a_use_is_not_re_extracted(self):
        self.two_callers()
        factor.apply(factor.proposals()[0])
        records.invalidate()
        self.assertEqual(factor.proposals(), [])

    def test_proposals_do_not_overlap(self):
        self.write(APP, "one", WALK + WALK)
        self.write(APP, "two", WALK + WALK)
        found = factor.proposals()
        spans = [(o["ref"], o["start"], entry["length"]) for entry in found for o in entry["occurrences"]]
        for left in spans:
            for right in spans:
                if left is right or left[0] != right[0]:
                    continue
                self.assertTrue(left[1] + left[2] <= right[1] or right[1] + right[2] <= left[1])

    def test_two_occurrences_inside_one_record_never_overlap(self):
        first, second = self.alphabet(2)
        self.write(APP, "caller", [first, second, first, second, first])
        self.write(APP, "other", [first, second, first])
        found = factor.proposals()
        self.assertEqual(len(found), 1)
        self.assertEqual(
            sorted((o["ref"], o["start"]) for o in found[0]["occurrences"]),
            [(records.qualify(APP, "caller"), 0), (records.qualify(APP, "other"), 0)],
        )
        outcome = factor.apply(found[0])
        self.assertEqual(records.load(APP, "caller")["steps"],
                         [{"op": "use", "ref": outcome["ref"]}, second, first])

    def test_an_overlapping_occurrence_does_not_swallow_the_records_other_steps(self):
        first, second = self.alphabet(2)
        tail_key = timing.entry_key([4], first)
        self.write(APP, "caller", [first, second, first, second, first],
                   timing={tail_key: [42]})
        self.write(APP, "other", [first, second, first])
        factor.apply(factor.proposals()[0])
        caller = records.load(APP, "caller")
        self.assertEqual(len(caller["steps"]), 3)
        self.assertEqual(caller["timing"][timing.entry_key([2], first)], [42])

    def test_a_conflicting_occurrence_drops_only_itself_not_the_whole_proposal(self):
        steps = self.alphabet(5)
        self.write(APP, "w", steps)
        self.write(APP, "x", steps)
        self.write(APP, "y", steps[:3])
        self.write(APP, "z", steps[:3])
        found = factor.proposals()
        self.assertEqual([entry["length"] for entry in found], [5, 3])
        self.assertEqual({o["ref"] for o in found[1]["occurrences"]},
                         {records.qualify(APP, "y"), records.qualify(APP, "z")})

    def test_an_existing_record_is_never_overwritten(self):
        self.write(SETTINGS, "general", [{"op": "tap", "sel": {"label": "unrelated"}}])
        self.two_callers()
        proposal = factor.proposals()[0]
        self.assertEqual(proposal["name"], "general")
        outcome = factor.apply(proposal)
        self.assertNotEqual(outcome["name"], "general")
        survivor = records.load(SETTINGS, "general")
        self.assertEqual(survivor["steps"], [{"op": "tap", "sel": {"label": "unrelated"}}])

    def test_an_explicit_name_that_is_taken_raises(self):
        self.write(SETTINGS, "taken", [{"op": "tap"}])
        self.two_callers()
        with self.assertRaises(factor.NameTaken):
            factor.apply(factor.proposals()[0], name="taken")

    def test_two_proposals_in_one_run_cannot_claim_the_same_name(self):
        self.two_callers()
        proposal = factor.proposals()[0]
        first = factor.apply(proposal, taken=set())
        self.assertNotIn(first["name"], ("",))
        self.assertEqual(factor.free_name(SETTINGS, "general", WALK, taken={"general"}),
                         factor.free_name(SETTINGS, "general", WALK, taken={"general"}))
        self.assertNotEqual(factor.free_name(SETTINGS, "x", WALK, taken={"x"}), "x")

    def test_a_record_containing_the_run_twice_keeps_every_other_step(self):
        tail = {"op": "tap", "sel": {"label": "z"}}
        self.write(APP, "one", WALK + [tail] + WALK)
        self.write(APP, "two", WALK + [{"op": "tap", "sel": {"label": "q"}}])
        outcome = factor.apply(factor.proposals()[0])
        caller = records.load(APP, "one")
        self.assertEqual(caller["steps"], [
            {"op": "use", "ref": outcome["ref"]}, tail, {"op": "use", "ref": outcome["ref"]},
        ])

    def test_the_fragment_exists_before_any_caller_points_at_it(self):
        self.two_callers()
        outcome = factor.apply(factor.proposals()[0])
        for path in outcome["rewrote"]:
            with open(path) as handle:
                self.assertIn(outcome["ref"], handle.read())
        self.assertTrue(os.path.exists(outcome["fragment"]))

    def test_suggest_name_is_kebab_case_from_the_first_label(self):
        self.assertEqual(factor.suggest_name(WALK), "general")
        self.assertTrue(factor.suggest_name([{"op": "tap"}]).startswith("fragment-"))


class FactorCommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-factor-cmd-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()

    def write(self, name, steps):
        directory = os.path.join(self.dir, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump({"app": APP, "name": name, "steps": steps}, handle)
        records.invalidate()

    def capture(self, argv):
        parser = argparse.ArgumentParser()
        factor_command.add_arguments(parser)
        buffer, saved = io.StringIO(), sys.stdout
        sys.stdout = buffer
        try:
            code = factor_command.run(parser.parse_args(argv))
        finally:
            sys.stdout = saved
        return code, buffer.getvalue()

    def test_name_is_spent_on_the_first_extraction_only(self):
        letters = [{"op": "tap", "sel": {"app": APP, "type": "button", "label": chr(97 + n)}}
                   for n in range(6)]
        self.write("one", letters[:5])
        self.write("two", letters[:5])
        self.write("three", [letters[5]] + letters[:3])
        code, text = self.capture([APP, "--apply", "--name", "myfrag"])
        self.assertEqual(code, 0)
        self.assertNotIn("skipped:", text)
        self.assertEqual(records.load(APP, "one")["steps"],
                         [{"op": "use", "ref": records.qualify(APP, "myfrag")}])
        second = records.load(APP, "three")["steps"][1]
        self.assertEqual(second["op"], "use")
        self.assertNotEqual(second["ref"], records.qualify(APP, "myfrag"))
        self.assertEqual(records.load_ref(second["ref"])["steps"], letters[:3])


class FactorWithCallerSourcesTests(unittest.TestCase):
    def setUp(self):
        self.builtin = tempfile.mkdtemp(prefix="uiflow-factor-builtin-")
        self.external = tempfile.mkdtemp(prefix="uiflow-factor-external-")
        self.addCleanup(shutil.rmtree, self.builtin, True)
        self.addCleanup(shutil.rmtree, self.external, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.builtin
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        sources.reset()
        self.addCleanup(sources.reset)
        self.addCleanup(records.invalidate)

    def write_builtin(self, name, steps):
        directory = os.path.join(self.builtin, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump({"app": APP, "name": name, "steps": steps}, handle)
        records.invalidate()

    def write_external(self, name, steps):
        directory = os.path.join(self.external, APP)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, name + ".json")
        with open(target, "w") as handle:
            json.dump({"app": APP, "name": name, "steps": steps}, handle)
        sources.register(target)
        return target

    def test_apply_factors_the_builtin_a_caller_source_shadows(self):
        tail = {"op": "tap", "sel": {"label": "z"}}
        self.write_builtin("one", WALK + [tail])
        self.write_builtin("two", WALK + [{"op": "tap", "sel": {"label": "q"}}])
        target = self.write_external("one", [{"op": "tap", "sel": {"label": "EXTERNAL-ONLY"}}])
        before = open(target).read()
        outcome = factor.apply(factor.proposals()[0])
        with open(outcome["fragment"]) as handle:
            self.assertEqual(json.load(handle)["steps"], WALK)
        with open(os.path.join(self.builtin, APP, "one.json")) as handle:
            self.assertEqual(json.load(handle)["steps"],
                             [{"op": "use", "ref": outcome["ref"]}, tail])
        self.assertEqual(open(target).read(), before)


if __name__ == "__main__":
    unittest.main()
