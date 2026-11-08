import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import records

APP = "com.example.Demo"


def record(name, **extra):
    payload = {"app": APP, "name": name, "steps": [{"op": "tap", "sel": {"label": name}}]}
    payload.update(extra)
    return payload


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-records-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        saved = records.paths.RECORDS_DIR
        records.paths.RECORDS_DIR = self.dir
        self.addCleanup(setattr, records.paths, "RECORDS_DIR", saved)
        records.invalidate()

    def write(self, payload):
        directory = os.path.join(self.dir, payload["app"])
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, payload["name"] + ".json"), "w") as handle:
            json.dump(payload, handle)
        records.invalidate()

    def test_safe_rejects_traversal(self):
        for bad in ("../evil", "a/b", "", ".", "..", "with space"):
            with self.assertRaises(records.BadName):
                records._safe(bad)
        self.assertEqual(records._safe("add-korean"), "add-korean")

    def test_missing_record_names_itself(self):
        self.write(record("present"))
        with self.assertRaises(records.MissingRecord) as caught:
            records.load(APP, "absent")
        self.assertIn("absent", str(caught.exception))
        self.assertIn("present", str(caught.exception))

    def test_alias_lookup(self):
        self.write(record("add-korean", aliases=["add korean", "korean"]))
        self.assertEqual(records.find_by_alias(APP, "add korean")["name"], "add-korean")
        self.assertEqual(records.find_by_alias(APP, "ADD KOREAN")["name"], "add-korean")
        self.assertIsNone(records.find_by_alias(APP, "nothing"))

    def test_requires_ordered_before_dependents(self):
        self.write(record("base"))
        self.write(record("middle", requires=["base"]))
        self.write(record("leaf", requires=["middle"]))
        self.assertEqual([r["name"] for r in records.resolve(APP, ["leaf"])], ["base", "middle", "leaf"])

    def test_shared_dependency_is_deduped(self):
        self.write(record("base"))
        self.write(record("a", requires=["base"]))
        self.write(record("b", requires=["base"]))
        names = [r["name"] for r in records.resolve(APP, ["a", "b"])]
        self.assertEqual(names, ["base", "a", "b"])

    def test_cycle_raises(self):
        self.write(record("a", requires=["b"]))
        self.write(record("b", requires=["a"]))
        with self.assertRaises(records.CycleError):
            records.resolve(APP, ["a"])

    def test_resolve_accepts_aliases(self):
        self.write(record("add-korean", aliases=["add korean"]))
        self.assertEqual([r["name"] for r in records.resolve(APP, ["add korean"])], ["add-korean"])

    def test_to_group_shape(self):
        payload = record(
            "x",
            satisfiedWhen=[{"op": "assert", "sel": {"label": "done"}}],
            verifies=[{"op": "assert", "sel": {"label": "ok"}}],
        )
        group = records.to_group(payload)
        self.assertEqual(group["op"], "group")
        self.assertEqual(group["name"], "x")
        self.assertEqual(group["steps"], payload["steps"])
        self.assertEqual(group["satisfiedWhen"], payload["satisfiedWhen"])
        self.assertEqual(group["verifies"], payload["verifies"])

    def test_to_group_omits_empty_keys(self):
        group = records.to_group(record("x"))
        self.assertNotIn("satisfiedWhen", group)
        self.assertNotIn("verifies", group)

    def test_save_round_trips_and_invalidates_the_cache(self):
        self.write(record("x"))
        self.assertEqual(len(records.all_for(APP)), 1)
        records.save(record("y"))
        self.assertEqual(sorted(records.all_for(APP)), ["x", "y"])
        self.assertEqual(records.load(APP, "y")["name"], "y")

    def test_all_for_is_memoized(self):
        self.write(record("x"))
        first = records.all_for(APP)
        self.assertIs(records.all_for(APP), first)

    def test_parse_ref_qualified_and_bare(self):
        self.assertEqual(records.parse_ref("com.apple.Preferences/open-x"), ("com.apple.Preferences", "open-x"))
        self.assertEqual(records.parse_ref("open-x", APP), (APP, "open-x"))

    def test_parse_ref_without_app_or_default_raises(self):
        with self.assertRaises(records.BadName):
            records.parse_ref("open-x")

    def test_parse_ref_rejects_traversal_in_either_half(self):
        for bad in ("../evil/x", "app/../x", "a/b/c/../d", "/x", "x/", "x\n", "app\n/x"):
            with self.assertRaises(records.BadName):
                records.parse_ref(bad, APP)

    def test_qualify_round_trips(self):
        self.assertEqual(records.parse_ref(records.qualify(APP, "n"), None), (APP, "n"))

    def test_load_ref_reaches_another_app(self):
        self.write({"app": "com.other.App", "name": "shared", "steps": []})
        self.assertEqual(records.load_ref("com.other.App/shared", APP)["name"], "shared")

    def test_canonical_ref_resolves_an_alias_to_the_real_name(self):
        self.write({"app": APP, "name": "initial-setup", "aliases": ["onboarding"], "steps": []})
        self.assertEqual(records.canonical_ref("onboarding", APP), records.qualify(APP, "initial-setup"))
        self.assertEqual(records.canonical_ref("initial-setup", APP), records.qualify(APP, "initial-setup"))

    def test_canonical_ref_raises_for_an_unknown_ref(self):
        with self.assertRaises(records.MissingRecord):
            records.canonical_ref("nope", APP)

    def test_kind_defaults_to_flow_and_rejects_junk(self):
        self.assertEqual(records.kind_of({}), records.FLOW)
        self.assertEqual(records.kind_of({"kind": "fragment"}), records.FRAGMENT)
        with self.assertRaises(records.BadName):
            records.kind_of({"kind": "gadget"})


class PlanSummaryTests(unittest.TestCase):
    def test_counts_ops_and_flags_launch_args_and_repairs(self):
        from uiflow.commands import do

        payload = {
            "name": "x",
            "steps": [
                {"op": "launch", "app": "com.example.Demo", "args": ["-UITestMode"]},
                {"op": "group", "steps": [{"op": "tap"}, {"op": "tap"}]},
            ],
            "health": {"repairedAt": "2026-09-01T00:00:00Z"},
        }
        self.assertEqual(do.count_ops(payload["steps"]), 4)
        line = do.plan_summary([payload])[0]
        self.assertIn("x: 4 ops", line)
        self.assertIn("-UITestMode", line)
        self.assertIn("auto-repaired", line)
        self.assertIn("no satisfiedWhen", line)


if __name__ == "__main__":
    unittest.main()
