import json
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import uiflow

if "uiflow.compose" not in sys.modules:
    def _use_refs(steps):
        found = []
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            if step.get("op") == "use" and step.get("ref") is not None:
                if step["ref"] not in found:
                    found.append(step["ref"])
            for inner in _use_refs(step.get("steps")):
                if inner not in found:
                    found.append(inner)
        return found

    def _refs_of(record):
        return _use_refs((record or {}).get("steps"))

    _stub = types.ModuleType("uiflow.compose")
    _stub.refs_of = _refs_of
    sys.modules["uiflow.compose"] = _stub
    uiflow.compose = _stub

from uiflow import index, records

APP = "com.example.Demo"
OTHER = "com.example.Other"
WALK = [
    {"op": "launch", "app": OTHER},
    {"op": "tap", "sel": {"app": OTHER, "type": "button", "label": "General"}},
    {"op": "tap", "sel": {"app": OTHER, "type": "button", "label": "Keyboard"}},
]


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-index-")
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

    def test_build_records_kind_uses_and_used_by(self):
        self.write(APP, "frag", [{"op": "tap"}], kind="fragment")
        self.write(APP, "caller", [{"op": "use", "ref": "frag"}], description="does a thing")
        payload = index.build()
        caller, frag = records.qualify(APP, "caller"), records.qualify(APP, "frag")
        self.assertEqual(payload["records"][frag]["kind"], "fragment")
        self.assertEqual(payload["records"][caller]["uses"], [frag])
        self.assertEqual(payload["records"][frag]["usedBy"], [caller])

    def test_a_use_step_hashes_as_itself_not_as_its_expansion(self):
        self.write(APP, "frag", WALK)
        self.write(APP, "caller", [{"op": "use", "ref": "frag"}])
        payload = index.build()
        use_hash = index.step_hash({"op": "use", "ref": "frag"})
        self.assertIn(use_hash, payload["steps"])
        self.assertNotEqual(use_hash, index.step_hash(WALK[0]))

    def test_a_learned_timeout_does_not_change_a_step_hash(self):
        bare = {"op": "tap", "sel": {"label": "a"}}
        self.assertEqual(index.step_hash(bare), index.step_hash(dict(bare, timeoutMs=4000)))

    def test_duplicate_runs_finds_a_shared_prefix_across_two_records(self):
        self.write(APP, "one", WALK + [{"op": "tap", "sel": {"label": "z"}}])
        self.write(APP, "two", WALK + [{"op": "tap", "sel": {"label": "q"}}])
        found = index.duplicate_runs(index.build())
        self.assertTrue(found)
        best = found[0]
        self.assertEqual(best["length"], 3)
        self.assertEqual(sorted(o["ref"] for o in best["occurrences"]),
                         sorted([records.qualify(APP, "one"), records.qualify(APP, "two")]))

    def test_a_run_shorter_than_min_run_is_not_reported(self):
        self.write(APP, "one", WALK[:2] + [{"op": "tap", "sel": {"label": "z"}}])
        self.write(APP, "two", WALK[:2] + [{"op": "tap", "sel": {"label": "q"}}])
        self.assertEqual(index.duplicate_runs(index.build(), min_run=3), [])

    def test_selectors_table_answers_who_taps_this_label(self):
        self.write(APP, "one", WALK)
        payload = index.build()
        hits = [ref for key, refs in payload["selectors"].items() if "General" in key for ref in refs]
        self.assertEqual(hits, [records.qualify(APP, "one")])

    def test_find_matches_name_alias_and_description(self):
        self.write(APP, "grant-full-access", [{"op": "tap"}], aliases=["full access"],
                   description="Turn on Allow Full Access")
        self.assertTrue(index.find("full access"))
        self.assertTrue(index.find("grant"))
        self.assertTrue(index.find("Allow Full"))
        self.assertEqual(index.find("nothing-like-this"), [])

    def test_find_matches_a_multi_word_query_in_any_order(self):
        self.write(APP, "open-keyboard-settings", WALK, description="Walk Settings to the page")
        for query in ("keyboard settings", "settings keyboard", "open settings"):
            self.assertTrue(index.find(query), "no hit for %r" % query)

    def test_find_requires_every_token_to_match(self):
        self.write(APP, "open-keyboard-settings", WALK, description="Walk Settings to the page")
        self.assertEqual(index.find("keyboard unicorn"), [])

    def test_find_can_be_scoped_to_one_app(self):
        self.write(APP, "shared-name", [{"op": "tap"}])
        self.write(OTHER, "shared-name", [{"op": "tap"}])
        self.assertEqual(len(index.find("shared-name")), 2)
        self.assertEqual(len(index.find("shared-name", app=APP)), 1)

    def test_find_matches_a_selector_label(self):
        self.write(APP, "one", WALK)
        self.assertTrue(index.find("Keyboard"))

    def test_load_rebuilds_when_a_record_changes(self):
        self.write(APP, "one", [{"op": "tap"}])
        self.assertEqual(index.load()["records"][records.qualify(APP, "one")]["stepCount"], 1)
        self.write(APP, "one", [{"op": "tap"}, {"op": "tap"}])
        self.assertEqual(index.load()["records"][records.qualify(APP, "one")]["stepCount"], 2)

    def test_load_rebuilds_a_corrupt_index(self):
        self.write(APP, "one", [{"op": "tap"}])
        with open(index.index_path(), "w") as handle:
            handle.write("{not json")
        self.assertIn(records.qualify(APP, "one"), index.load()["records"])

    def test_load_rebuilds_on_a_version_bump(self):
        self.write(APP, "one", [{"op": "tap"}])
        payload = index.load()
        payload["version"] = index.VERSION - 1
        payload["records"] = {}
        with open(index.index_path(), "w") as handle:
            json.dump(payload, handle)
        self.assertIn(records.qualify(APP, "one"), index.load()["records"])

    def test_one_malformed_record_is_skipped_not_fatal(self):
        self.write(APP, "good", [{"op": "tap"}])
        self.write(APP, "bad", [{"op": "tap"}], kind="Fragment")
        payload = index.build()
        self.assertIn(records.qualify(APP, "good"), payload["records"])
        self.assertIn(records.qualify(APP, "bad"), payload["skipped"])

    def test_an_alias_spelled_use_canonicalises_to_the_real_ref(self):
        self.write(APP, "initial-setup", [{"op": "tap"}], aliases=["onboarding"])
        self.write(APP, "caller", [{"op": "use", "ref": "onboarding"}])
        payload = index.build()
        target = records.qualify(APP, "initial-setup")
        self.assertEqual(payload["records"][records.qualify(APP, "caller")]["uses"], [target])
        self.assertEqual(payload["records"][target]["usedBy"], [records.qualify(APP, "caller")])

    def test_the_staleness_check_parses_no_record_json(self):
        self.write(APP, "one", [{"op": "tap"}])
        index.load()
        records.invalidate()
        opened = []
        real_open = open

        def spy(path, *rest, **kwargs):
            opened.append(str(path))
            return real_open(path, *rest, **kwargs)

        import builtins

        builtins.open = spy
        try:
            index.load()
        finally:
            builtins.open = real_open
        self.assertFalse([p for p in opened if p.endswith("one.json")])

    def test_the_index_file_is_not_itself_indexed(self):
        self.write(APP, "one", [{"op": "tap"}])
        index.load()
        self.assertEqual(list(index.build()["records"]), [records.qualify(APP, "one")])


if __name__ == "__main__":
    unittest.main()
