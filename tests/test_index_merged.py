import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import index, records, sources

APP = "com.example.Demo"


class MergedIndexTests(unittest.TestCase):
    def setUp(self):
        self.builtin = tempfile.mkdtemp(prefix="uiflow-index-builtin-")
        self.external = tempfile.mkdtemp(prefix="uiflow-index-external-")
        self.addCleanup(shutil.rmtree, self.builtin, True)
        self.addCleanup(shutil.rmtree, self.external, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.builtin
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()
        sources.reset()
        self.addCleanup(sources.reset)
        self.addCleanup(records.invalidate)

    def write_builtin(self, name, **extra):
        payload = {"app": APP, "name": name, "description": "a builtin walk",
                   "steps": [{"op": "tap", "sel": {"type": "button", "label": "Native"}}]}
        payload.update(extra)
        directory = os.path.join(self.builtin, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump(payload, handle)
        records.invalidate()

    def write_external(self, name, **extra):
        payload = {"app": APP, "name": name, "description": "a caller walk",
                   "steps": [{"op": "tap", "sel": {"type": "button", "label": "Caller"}}]}
        payload.update(extra)
        directory = os.path.join(self.external, APP)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, name + ".json")
        with open(target, "w") as handle:
            json.dump(payload, handle)
        sources.register(target)
        return target

    def test_build_from_indexes_a_ref_mapping(self):
        payload = index.build_from({"%s/x" % APP: {"app": APP, "name": "x", "steps": [{"op": "tap"}]}})
        self.assertIn("%s/x" % APP, payload["records"])
        self.assertEqual(payload["stamps"], {})
        self.assertEqual(payload["records"]["%s/x" % APP]["stepCount"], 1)

    def test_merged_carries_both_builtin_and_external_records(self):
        self.write_builtin("native")
        self.write_external("onboarding")
        payload = index.merged()
        self.assertIn("%s/native" % APP, payload["records"])
        self.assertIn("%s/onboarding" % APP, payload["records"])

    def test_merged_lets_an_external_record_shadow_a_builtin(self):
        self.write_builtin("shared")
        self.write_external("shared")
        entry = index.merged()["records"]["%s/shared" % APP]
        self.assertEqual(entry["description"], "a caller walk")

    def test_merged_relinks_used_by_across_the_two_sources(self):
        self.write_builtin("frag", kind="fragment")
        self.write_external("caller", steps=[{"op": "use", "ref": "frag"}])
        payload = index.merged()
        self.assertIn("%s/caller" % APP, payload["records"]["%s/frag" % APP]["usedBy"])

    def test_merged_is_the_on_disk_payload_when_nothing_is_registered(self):
        self.write_builtin("native")
        base = index.load()
        self.assertIs(index.merged(base), base)

    def test_build_excludes_a_registered_external(self):
        self.write_builtin("native")
        self.write_external("onboarding")
        self.assertEqual(list(index.build()["records"]), ["%s/native" % APP])

    def test_a_record_declaring_a_traversing_name_is_skipped_not_indexed(self):
        directory = os.path.join(self.builtin, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "evil.json"), "w") as handle:
            json.dump({"app": APP, "name": "../evil", "steps": []}, handle)
        records.invalidate()
        payload = index.build()
        self.assertEqual(list(payload["records"]), [])
        self.assertEqual(len(payload["skipped"]), 1)

    def test_a_shadowed_builtin_leaves_no_selector_behind(self):
        self.write_builtin("shared")
        self.write_external("shared")
        payload = index.merged()
        self.assertEqual(index.find("native", payload=payload), [])
        self.assertEqual([hit["ref"] for hit in index.find("caller", payload=payload)],
                         ["%s/shared" % APP])

    def test_merged_never_writes_externals_to_the_index_file(self):
        self.write_builtin("native")
        self.write_external("onboarding")
        index.merged()
        with open(index.index_path()) as handle:
            self.assertNotIn("%s/onboarding" % APP, json.load(handle)["records"])

    def test_a_merged_payload_carries_no_stamps_so_it_cannot_look_fresh(self):
        self.write_builtin("native")
        self.write_external("onboarding")
        merged = index.merged()
        self.assertEqual(merged["stamps"], {})
        self.assertNotEqual(index.load()["stamps"], {})

    def test_find_over_the_merged_payload_hits_an_external_record(self):
        self.write_external("onboarding")
        hits = index.find("onboarding", payload=index.merged())
        self.assertEqual([hit["ref"] for hit in hits], ["%s/onboarding" % APP])

    def test_build_resolves_a_use_edge_against_builtins_only(self):
        self.write_builtin("caller", steps=[{"op": "use", "ref": "frag"}])
        self.write_external("frag-v2", aliases=["frag"])
        self.assertEqual(index.build()["records"]["%s/caller" % APP]["uses"], ["%s/frag" % APP])
        index.load()
        with open(index.index_path()) as handle:
            written = json.load(handle)
        self.assertEqual(written["records"]["%s/caller" % APP]["uses"], ["%s/frag" % APP])

    def test_merged_still_resolves_a_use_edge_across_the_two_sources(self):
        self.write_external("frag-v2", aliases=["frag"])
        self.write_external("caller", steps=[{"op": "use", "ref": "frag"}])
        payload = index.merged()
        self.assertEqual(payload["records"]["%s/caller" % APP]["uses"], ["%s/frag-v2" % APP])


if __name__ == "__main__":
    unittest.main()
