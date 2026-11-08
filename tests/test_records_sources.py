import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import records, sources

APP = "com.example.Demo"


class RecordsWithSourcesTests(unittest.TestCase):
    def setUp(self):
        self.builtin = tempfile.mkdtemp(prefix="uiflow-builtin-")
        self.external = tempfile.mkdtemp(prefix="uiflow-external-")
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
        payload = {"app": APP, "name": name, "steps": [{"op": "tap", "sel": {"label": "builtin"}}]}
        payload.update(extra)
        directory = os.path.join(self.builtin, APP)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name + ".json"), "w") as handle:
            json.dump(payload, handle)
        records.invalidate()
        return payload

    def write_external(self, name, **extra):
        payload = {"app": APP, "name": name, "steps": [{"op": "tap", "sel": {"label": "external"}}]}
        payload.update(extra)
        directory = os.path.join(self.external, APP)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, name + ".json")
        with open(target, "w") as handle:
            json.dump(payload, handle)
        return target

    def test_an_external_record_is_visible_alongside_the_builtins(self):
        self.write_builtin("native")
        sources.register(self.write_external("onboarding"))
        self.assertEqual(sorted(records.all_for(APP)), ["native", "onboarding"])
        self.assertEqual(records.load(APP, "onboarding")["steps"][0]["sel"]["label"], "external")

    def test_an_external_record_shadows_a_builtin_of_the_same_name(self):
        self.write_builtin("shared")
        sources.register(self.write_external("shared"))
        self.assertEqual(records.load(APP, "shared")["steps"][0]["sel"]["label"], "external")

    def test_all_for_is_still_memoized_when_nothing_is_registered(self):
        self.write_builtin("native")
        first = records.all_for(APP)
        self.assertIs(records.all_for(APP), first)

    def test_save_writes_an_external_record_back_to_its_own_file(self):
        target = self.write_external("onboarding")
        sources.register(target)
        loaded = records.load(APP, "onboarding")
        loaded["timing"] = {"0|abc": {"hits": [120], "misses": 0}}
        written = records.save(loaded)
        self.assertEqual(written, target)
        with open(target) as handle:
            self.assertEqual(json.load(handle)["timing"], {"0|abc": {"hits": [120], "misses": 0}})
        self.assertFalse(os.path.exists(os.path.join(self.builtin, APP, "onboarding.json")))

    def test_a_non_external_save_never_writes_into_a_registered_caller_file(self):
        target = self.write_external("collide")
        sources.register(target)
        before = open(target).read()
        fresh = {"app": APP, "name": "collide", "steps": [{"op": "tap", "sel": {"label": "new"}}]}
        written = records.save(fresh, external=False)
        self.assertEqual(written, os.path.join(self.builtin, APP, "collide.json"))
        self.assertEqual(open(target).read(), before)

    def test_builtins_for_never_sees_an_external(self):
        self.write_builtin("native")
        sources.register(self.write_external("onboarding"))
        self.assertEqual(sorted(records.builtins_for(APP)), ["native"])

    def test_shadowed_names_the_refs_that_override_a_builtin(self):
        self.write_builtin("shared")
        sources.register(self.write_external("shared"))
        sources.register(self.write_external("fresh"))
        self.assertEqual(records.shadowed(sources.refs()), ["%s/shared" % APP])

    def test_save_still_writes_a_builtin_to_the_builtin_root(self):
        self.write_builtin("native")
        loaded = records.load(APP, "native")
        written = records.save(loaded)
        self.assertEqual(written, os.path.join(self.builtin, APP, "native.json"))

    def test_save_of_an_external_record_is_visible_on_the_next_load(self):
        sources.register(self.write_external("onboarding"))
        loaded = records.load(APP, "onboarding")
        loaded["health"] = {"runs": 3, "failures": 0, "lastOk": "now", "repairedAt": None}
        records.save(loaded)
        self.assertEqual(records.load(APP, "onboarding")["health"]["runs"], 3)

    def test_an_external_record_resolves_a_use_of_a_builtin_fragment(self):
        from uiflow import compose

        self.write_builtin("frag", kind="fragment")
        sources.register(self.write_external("caller", steps=[{"op": "use", "ref": "frag"}]))
        steps, _origins, _owners = compose.expand(records.load(APP, "caller"), APP)
        self.assertEqual(steps[0]["name"], "use:%s/frag" % APP)

    def test_a_builtin_resolves_a_use_of_an_external_record(self):
        from uiflow import compose

        sources.register(self.write_external("frag", kind="fragment"))
        self.write_builtin("caller", steps=[{"op": "use", "ref": "frag"}])
        steps, _origins, _owners = compose.expand(records.load(APP, "caller"), APP)
        self.assertEqual(steps[0]["steps"][0]["sel"]["label"], "external")

    def test_missing_record_names_the_externals_too(self):
        self.write_builtin("native")
        sources.register(self.write_external("onboarding"))
        with self.assertRaises(records.MissingRecord) as caught:
            records.load(APP, "absent")
        message = str(caught.exception)
        self.assertIn("native", message)
        self.assertIn("onboarding", message)

    def test_missing_record_points_at_the_same_name_under_another_app(self):
        directory = os.path.join(self.external, "com.other.App")
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, "onboarding.json")
        with open(target, "w") as handle:
            json.dump({"app": "com.other.App", "name": "onboarding", "steps": []}, handle)
        sources.register(target)
        with self.assertRaises(records.MissingRecord) as caught:
            records.load(APP, "onboarding")
        self.assertIn("com.other.App/onboarding", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
