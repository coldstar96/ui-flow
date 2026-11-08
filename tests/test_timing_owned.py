import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import timing

CALLER = "com.example.Demo/caller"
FRAG = "com.example.Demo/frag"


def step_result(index, op, ms, ok=True, children=None):
    payload = {"index": index, "op": op, "ms": ms, "ok": ok}
    if children is not None:
        payload["children"] = children
    return payload


class CollectTests(unittest.TestCase):
    def test_samples_are_credited_to_the_owning_record(self):
        steps = [
            {"op": "launch", "app": "a"},
            {"op": "group", "name": "use:frag", "steps": [{"op": "tap", "sel": {"label": "x"}}]},
        ]
        origins = {
            (0,): (CALLER, (0,)),
            (1,): (CALLER, (1,)),
            (1, 0): (FRAG, (0,)),
        }
        result = {"children": [
            step_result(0, "launch", 20),
            step_result(1, "group", 90, children=[step_result(0, "tap", 70)]),
        ]}
        collected = timing.collect(steps, result, origins)
        self.assertEqual(collected[CALLER][timing.entry_key([0], steps[0])], 20)
        self.assertEqual(collected[FRAG][timing.entry_key([0], {"op": "tap", "sel": {"label": "x"}})], 70)
        self.assertNotIn(timing.entry_key([1, 0], steps[1]["steps"][0]), collected[CALLER])

    def test_a_skipped_step_contributes_no_sample(self):
        steps = [{"op": "tap", "sel": {"label": "x"}}]
        origins = {(0,): (CALLER, (0,))}
        result = {"children": [dict(step_result(0, "tap", 5), skipped=True)]}
        self.assertEqual(timing.collect(steps, result, origins), {})

    def test_a_failed_step_contributes_no_sample(self):
        steps = [{"op": "tap", "sel": {"label": "x"}}]
        origins = {(0,): (CALLER, (0,))}
        result = {"children": [step_result(0, "tap", 5, ok=False)]}
        self.assertEqual(timing.collect(steps, result, origins), {})

    def test_missing_children_are_survivable(self):
        steps = [{"op": "group", "steps": [{"op": "tap"}]}]
        origins = {(0,): (CALLER, (0,)), (0, 0): (FRAG, (0,))}
        collected = timing.collect(steps, {"children": [step_result(0, "group", 9)]}, origins)
        self.assertEqual(collected, {CALLER: {timing.entry_key([0], steps[0]): 9}})
        self.assertNotIn(FRAG, collected)

    def test_a_use_step_is_keyed_by_the_owners_own_step(self):
        use_step = {"op": "use", "ref": "frag"}
        expanded = {"op": "group", "name": "use:frag", "steps": [{"op": "tap", "sel": {"label": "x"}}]}
        origins = {(0,): (CALLER, (0,)), (0, 0): (FRAG, (0,))}
        owners = {(0,): use_step, (0, 0): {"op": "tap", "sel": {"label": "x"}}}
        result = {"children": [step_result(0, "group", 80, children=[step_result(0, "tap", 60)])]}
        collected = timing.collect([expanded], result, origins, owners=owners)
        self.assertIn(timing.entry_key([0], use_step), collected[CALLER])
        self.assertNotIn(timing.entry_key([0], expanded), collected[CALLER])

    def test_a_reassert_result_does_not_shift_sample_pairing(self):
        steps = [{"op": "tap", "sel": {"label": "a"}}, {"op": "tap", "sel": {"label": "b"}}]
        origins = {(0,): (CALLER, (0,)), (1,): (CALLER, (1,))}
        result = {"children": [
            step_result(0, "tap", 11),
            dict(step_result(-1, "assert", 3)),
            step_result(1, "tap", 22),
        ]}
        collected = timing.collect(steps, result, origins)
        self.assertEqual(collected[CALLER][timing.entry_key([0], steps[0])], 11)
        self.assertEqual(collected[CALLER][timing.entry_key([1], steps[1])], 22)

    def test_learn_still_folds_into_the_record_itself(self):
        record = {"app": "com.example.Demo", "name": "one",
                  "steps": [{"op": "tap", "sel": {"label": "x"}}], "timing": {}}
        learned = timing.learn(record, {"children": [step_result(0, "tap", 45)]})
        self.assertEqual(timing.samples(learned, [0], record["steps"][0]), [45])

    def test_merge_appends_and_caps_samples(self):
        record = {"app": "com.example.Demo", "name": "frag", "steps": [{"op": "tap", "sel": {"label": "x"}}],
                  "timing": {}}
        key = timing.entry_key([0], record["steps"][0])
        for value in range(timing.MAX_SAMPLES + 5):
            record = timing.merge(record, {key: value})
        stored = timing.samples(record, [0], record["steps"][0])
        self.assertEqual(len(stored), timing.MAX_SAMPLES)
        self.assertEqual(stored[-1], timing.MAX_SAMPLES + 4)

    def test_merge_drops_keys_that_are_no_longer_live(self):
        record = {"app": "com.example.Demo", "name": "frag", "steps": [{"op": "tap", "sel": {"label": "x"}}],
                  "timing": {"9.9|deadbeef": [12]}}
        merged = timing.merge(record, {})
        self.assertNotIn("9.9|deadbeef", merged["timing"])


if __name__ == "__main__":
    unittest.main()
