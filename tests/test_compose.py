import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import compose, records

APP = "com.example.Demo"
OTHER = "com.example.Other"


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uiflow-compose-")
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

    def test_a_record_without_use_is_unchanged(self):
        payload = self.write(APP, "plain", [{"op": "tap", "sel": {"label": "a"}}])
        steps, origins, _ = compose.expand(payload, APP)
        self.assertEqual(steps, payload["steps"])
        self.assertEqual(origins[(0,)], (records.qualify(APP, "plain"), (0,)))

    def test_use_inlines_a_group_carrying_the_fragments_own_preconditions(self):
        self.write(
            APP, "frag", [{"op": "tap", "sel": {"label": "x"}}],
            kind="fragment", satisfiedWhen=[{"op": "assert", "sel": {"label": "done"}}],
            verifies=[{"op": "assert", "sel": {"label": "v"}}],
        )
        caller = self.write(APP, "caller", [
            {"op": "launch", "app": APP},
            {"op": "use", "ref": "frag"},
        ])
        steps, _, _ = compose.expand(caller, APP)
        self.assertEqual(steps[0]["op"], "launch")
        self.assertEqual(steps[1]["op"], "group")
        self.assertEqual(steps[1]["name"], "use:%s" % records.qualify(APP, "frag"))
        self.assertEqual(steps[1]["steps"], [{"op": "tap", "sel": {"label": "x"}}])
        self.assertEqual(steps[1]["satisfiedWhen"], [{"op": "assert", "sel": {"label": "done"}}])
        self.assertEqual(steps[1]["verifies"], [{"op": "assert", "sel": {"label": "v"}}])

    def test_origins_credit_inlined_steps_to_the_fragment(self):
        self.write(APP, "frag", [{"op": "tap", "sel": {"label": "x"}}, {"op": "tap", "sel": {"label": "y"}}])
        caller = self.write(APP, "caller", [{"op": "launch", "app": APP}, {"op": "use", "ref": "frag"}])
        _, origins, _ = compose.expand(caller, APP)
        caller_ref, frag_ref = records.qualify(APP, "caller"), records.qualify(APP, "frag")
        self.assertEqual(origins[(0,)], (caller_ref, (0,)))
        self.assertEqual(origins[(1,)], (caller_ref, (1,)))
        self.assertEqual(origins[(1, 0)], (frag_ref, (0,)))
        self.assertEqual(origins[(1, 1)], (frag_ref, (1,)))

    def test_a_qualified_ref_reaches_another_app(self):
        self.write(OTHER, "shared", [{"op": "tap", "sel": {"label": "s"}}])
        caller = self.write(APP, "caller", [{"op": "use", "ref": "%s/shared" % OTHER}])
        steps, origins, _ = compose.expand(caller, APP)
        self.assertEqual(steps[0]["steps"], [{"op": "tap", "sel": {"label": "s"}}])
        self.assertEqual(origins[(0, 0)], (records.qualify(OTHER, "shared"), (0,)))

    def test_nesting_credits_the_deepest_owner(self):
        self.write(APP, "inner", [{"op": "tap", "sel": {"label": "i"}}])
        self.write(APP, "outer", [{"op": "use", "ref": "inner"}])
        caller = self.write(APP, "caller", [{"op": "use", "ref": "outer"}])
        _, origins, _ = compose.expand(caller, APP)
        self.assertEqual(origins[(0, 0, 0)], (records.qualify(APP, "inner"), (0,)))
        self.assertEqual(origins[(0, 0)], (records.qualify(APP, "outer"), (0,)))

    def test_use_inside_a_repeat_until_is_expanded_and_mapped(self):
        self.write(APP, "frag", [{"op": "tap", "sel": {"label": "x"}}])
        caller = self.write(APP, "caller", [
            {"op": "repeatUntil", "steps": [{"op": "use", "ref": "frag"}],
             "until": [{"op": "assert", "sel": {"label": "done"}}]},
        ])
        steps, origins, _ = compose.expand(caller, APP)
        self.assertEqual(steps[0]["steps"][0]["op"], "group")
        self.assertEqual(origins[(0, 0, 0)], (records.qualify(APP, "frag"), (0,)))

    def test_a_cycle_raises(self):
        self.write(APP, "a", [{"op": "use", "ref": "b"}])
        self.write(APP, "b", [{"op": "use", "ref": "a"}])
        with self.assertRaises(records.CycleError):
            compose.expand(records.load(APP, "a"), APP)

    def test_depth_beyond_the_cap_raises(self):
        for level in range(compose.MAX_DEPTH + 2):
            nxt = [{"op": "use", "ref": "d%d" % (level + 1)}] if level < compose.MAX_DEPTH + 1 else [{"op": "tap"}]
            self.write(APP, "d%d" % level, nxt)
        with self.assertRaises(compose.DepthError):
            compose.expand(records.load(APP, "d0"), APP)

    def test_use_in_a_probe_list_is_refused(self):
        self.write(APP, "frag", [{"op": "tap"}])
        for key in ("satisfiedWhen", "verifies", "reassert", "until"):
            caller = self.write(APP, "bad-" + key, [{"op": "tap"}], **{key: [{"op": "use", "ref": "frag"}]})
            with self.assertRaises(compose.MisplacedUseError):
                compose.expand(caller, APP)

    def test_a_missing_ref_raises_missing_record(self):
        caller = self.write(APP, "caller", [{"op": "use", "ref": "nope"}])
        with self.assertRaises(records.MissingRecord):
            compose.expand(caller, APP)

    def test_prepare_is_applied_to_every_loaded_record(self):
        self.write(APP, "frag", [{"op": "tap", "sel": {"label": "x"}}])
        caller = self.write(APP, "caller", [{"op": "use", "ref": "frag"}])
        seen = []

        def prepare(record):
            seen.append(record["name"])
            return record

        compose.expand(caller, APP, prepare=prepare)
        self.assertEqual(sorted(seen), ["caller", "frag"])

    def test_refs_of_lists_document_order_deduped(self):
        payload = {"app": APP, "name": "c", "steps": [
            {"op": "use", "ref": "b"},
            {"op": "repeatUntil", "steps": [{"op": "use", "ref": "a"}], "until": []},
            {"op": "use", "ref": "b"},
        ]}
        self.assertEqual(compose.refs_of(payload), ["b", "a"])

    def test_an_inlined_record_with_requires_is_refused(self):
        self.write(APP, "frag", [{"op": "tap"}], requires=["other"])
        caller = self.write(APP, "caller", [{"op": "use", "ref": "frag"}])
        with self.assertRaises(compose.UnsatisfiableRequiresError):
            compose.expand(caller, APP)

    def test_a_top_level_record_may_still_carry_requires(self):
        top = self.write(APP, "top", [{"op": "tap"}], requires=["other"])
        steps, _, _ = compose.expand(top, APP)
        self.assertEqual(steps, [{"op": "tap"}])

    def test_owners_records_the_step_before_expansion(self):
        self.write(APP, "frag", [{"op": "tap", "sel": {"label": "x"}}])
        caller = self.write(APP, "caller", [{"op": "use", "ref": "frag"}])
        _, _, owners = compose.expand(caller, APP)
        self.assertEqual(owners[(0,)], {"op": "use", "ref": "frag"})
        self.assertEqual(owners[(0, 0)], {"op": "tap", "sel": {"label": "x"}})

    def test_expanded_group_wraps_the_record(self):
        self.write(APP, "frag", [{"op": "tap"}])
        caller = self.write(APP, "caller", [{"op": "use", "ref": "frag"}],
                            satisfiedWhen=[{"op": "assert", "sel": {"label": "q"}}])
        group, origins, _ = compose.expanded_group(caller, APP)
        self.assertEqual(group["op"], "group")
        self.assertEqual(group["name"], "caller")
        self.assertEqual(group["satisfiedWhen"], [{"op": "assert", "sel": {"label": "q"}}])
        self.assertEqual(group["steps"][0]["op"], "group")
        self.assertIn((0, 0), origins)


if __name__ == "__main__":
    unittest.main()
