import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import timing


def record_with(steps, table=None):
    return {"app": "com.example.Demo", "name": "x", "steps": steps, "timing": table or {}}


def group_result(children, ok=True):
    return {"index": 0, "op": "group", "ms": 100, "ok": ok, "children": children}


def step_result(index, op, ms, ok=True, children=None):
    result = {"index": index, "op": op, "ms": ms, "ok": ok}
    if children is not None:
        result["children"] = children
    return result


class BudgetTests(unittest.TestCase):
    def test_formula(self):
        samples = [100, 100, 100, 100]
        self.assertEqual(timing.budget_ms(samples), max(400, min(15000, 100 * 3 + 250)))

    def test_floor(self):
        self.assertEqual(timing.budget_ms([1, 1, 1]), 400)

    def test_ceiling(self):
        self.assertEqual(timing.budget_ms([90000] * 5), 15000)

    def test_p95_is_nearest_rank(self):
        samples = [10, 10, 10, 10, 10, 10, 10, 10, 10, 900]
        self.assertEqual(timing.budget_ms(samples), min(15000, 900 * 3 + 250))


class FingerprintTests(unittest.TestCase):
    def test_ignores_timeout_ms(self):
        a = {"op": "tap", "sel": {"label": "Go"}, "timeoutMs": 100}
        b = {"op": "tap", "sel": {"label": "Go"}, "timeoutMs": 9000}
        self.assertEqual(timing.fingerprint(a), timing.fingerprint(b))

    def test_changes_with_the_selector(self):
        a = {"op": "tap", "sel": {"label": "Go"}}
        b = {"op": "tap", "sel": {"label": "Stop"}}
        self.assertNotEqual(timing.fingerprint(a), timing.fingerprint(b))

    def test_changes_with_the_op(self):
        self.assertNotEqual(
            timing.fingerprint({"op": "tap", "sel": {"label": "Go"}}),
            timing.fingerprint({"op": "assert", "sel": {"label": "Go"}}),
        )

    def test_key_ordering_does_not_matter(self):
        self.assertEqual(
            timing.fingerprint({"op": "tap", "sel": {"label": "Go", "type": "button"}}),
            timing.fingerprint({"sel": {"type": "button", "label": "Go"}, "op": "tap"}),
        )


class PathKeyTests(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(timing.path_key([0]), "0")
        self.assertEqual(timing.path_key([0, 2, 1]), "0.2.1")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.steps = [{"op": "tap", "sel": {"label": "Go"}}]

    def test_no_stamp_below_three_samples(self):
        for count in (0, 1, 2):
            record = record_with(copy.deepcopy(self.steps))
            for _ in range(count):
                record = timing.learn(record, group_result([step_result(0, "tap", 120)]))
            applied = timing.apply(record)
            self.assertNotIn("timeoutMs", applied["steps"][0], count)

    def test_stamps_at_three_samples(self):
        record = record_with(copy.deepcopy(self.steps))
        for _ in range(3):
            record = timing.learn(record, group_result([step_result(0, "tap", 120)]))
        applied = timing.apply(record)
        self.assertEqual(applied["steps"][0]["timeoutMs"], timing.budget_ms([120, 120, 120]))

    def test_apply_does_not_mutate_the_record(self):
        record = record_with(copy.deepcopy(self.steps))
        for _ in range(3):
            record = timing.learn(record, group_result([step_result(0, "tap", 120)]))
        timing.apply(record)
        self.assertNotIn("timeoutMs", record["steps"][0])

    def test_samples_from_a_different_selector_are_not_reused(self):
        record = record_with(copy.deepcopy(self.steps))
        for _ in range(4):
            record = timing.learn(record, group_result([step_result(0, "tap", 5000)]))
        record["steps"][0]["sel"]["label"] = "Stop"
        self.assertNotIn("timeoutMs", timing.apply(record)["steps"][0])


class LearnTests(unittest.TestCase):
    def test_folds_results_out_of_children(self):
        record = record_with([
            {"op": "tap", "sel": {"label": "a"}},
            {"op": "group", "steps": [{"op": "tap", "sel": {"label": "b"}}]},
        ])
        result = group_result([
            step_result(0, "tap", 40),
            step_result(1, "group", 90, children=[step_result(0, "tap", 70)]),
        ])
        record = timing.learn(record, result)
        self.assertEqual(timing.samples(record, [0], record["steps"][0]), [40])
        self.assertEqual(timing.samples(record, [1, 0], record["steps"][1]["steps"][0]), [70])

    def test_keys_are_path_prefixed(self):
        record = timing.learn(record_with([{"op": "tap", "sel": {"label": "a"}}]),
                              group_result([step_result(0, "tap", 40)]))
        self.assertTrue(all(key.split("|")[0] in ("0",) for key in record["timing"]), record["timing"])

    def test_keeps_the_last_twenty_samples(self):
        record = record_with([{"op": "tap", "sel": {"label": "a"}}])
        for value in range(30):
            record = timing.learn(record, group_result([step_result(0, "tap", value)]))
        stored = timing.samples(record, [0], record["steps"][0])
        self.assertEqual(len(stored), 20)
        self.assertEqual(stored[-1], 29)
        self.assertEqual(stored[0], 10)

    def test_skipped_and_failed_results_are_not_sampled(self):
        record = record_with([{"op": "tap", "sel": {"label": "a"}}])
        record = timing.learn(record, group_result([dict(step_result(0, "tap", 40), skipped=True)]))
        record = timing.learn(record, group_result([step_result(0, "tap", 40, ok=False)]))
        self.assertEqual(timing.samples(record, [0], record["steps"][0]), [])

    def test_orphan_entries_are_pruned(self):
        record = record_with([{"op": "tap", "sel": {"label": "a"}}])
        record["timing"]["9.9|deadbeef"] = [1, 2, 3]
        record = timing.learn(record, group_result([step_result(0, "tap", 40)]))
        self.assertNotIn("9.9|deadbeef", record["timing"])

    def test_extra_children_beyond_the_steps_are_ignored(self):
        record = record_with([{"op": "tap", "sel": {"label": "a"}}])
        record = timing.learn(record, group_result([step_result(0, "tap", 40), step_result(1, "assert", 10)]))
        self.assertEqual(len(record["timing"]), 1)

    def test_a_missing_children_list_is_survivable(self):
        record = record_with([{"op": "tap", "sel": {"label": "a"}}])
        self.assertEqual(timing.learn(record, {"index": 0, "op": "group", "ms": 5, "ok": True})["timing"], {})


class DoWiringTests(unittest.TestCase):
    def test_learning_bumps_health_and_saves_each_record(self):
        from uiflow.commands import do

        resolved = [record_with([{"op": "tap", "sel": {"label": "a"}}])]
        resolved[0]["name"] = "one"
        reply = {"ok": True, "results": [group_result([step_result(0, "tap", 40)])]}
        saved = []
        do.apply_learning(resolved, reply, save=saved.append)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["health"]["runs"], 1)
        self.assertEqual(saved[0]["health"]["failures"], 0)
        self.assertTrue(saved[0]["health"]["lastOk"])
        self.assertEqual(timing.samples(saved[0], [0], saved[0]["steps"][0]), [40])

    def test_a_failed_group_bumps_failures(self):
        from uiflow.commands import do

        resolved = [record_with([{"op": "tap", "sel": {"label": "a"}}])]
        reply = {"ok": False, "results": [group_result([step_result(0, "tap", 40, ok=False)], ok=False)]}
        saved = []
        do.apply_learning(resolved, reply, save=saved.append)
        self.assertEqual(saved[0]["health"]["failures"], 1)
        self.assertIsNone(saved[0]["health"]["lastOk"])

    def test_do_stamps_learned_budgets_into_the_batch(self):
        from uiflow.commands import do

        record = record_with([{"op": "tap", "sel": {"label": "a"}}])
        for _ in range(3):
            record = timing.learn(record, group_result([step_result(0, "tap", 120)]))
        group = records_to_group(timing.apply(record))
        self.assertEqual(group["steps"][0]["timeoutMs"], timing.budget_ms([120, 120, 120]))
        self.assertTrue(do.count_ops(group["steps"]) >= 1)



class ShimTests(unittest.TestCase):
    def test_a_pre_v3_list_is_read_as_hits(self):
        self.assertEqual(timing.unpack([1, 2, 3]), {"hits": [1, 2, 3]})

    def test_a_missing_entry_is_empty(self):
        self.assertEqual(timing.unpack(None), {})

    def test_samples_reads_through_the_shim(self):
        steps = [{"op": "tap", "sel": {"label": "a"}}]
        record = record_with(steps, {timing.entry_key([0], steps[0]): [7, 8]})
        self.assertEqual(timing.samples(record, [0], steps[0]), [7, 8])

    def test_merge_rewrites_a_list_entry_into_a_dict(self):
        steps = [{"op": "tap", "sel": {"label": "a"}}]
        record = record_with(steps, {timing.entry_key([0], steps[0]): [7, 8]})
        self.assertEqual(
            timing.merge(record, {})["timing"][timing.entry_key([0], steps[0])],
            {"hits": [7, 8]},
        )


class AbsentBudgetTests(unittest.TestCase):
    def test_decays_geometrically_then_floors(self):
        self.assertEqual(
            [timing.absent_budget(1800, n) for n in (4, 5, 6, 7)], [1080, 648, 400, 400]
        )

    def test_never_exceeds_the_authored_value(self):
        self.assertLessEqual(timing.absent_budget(500, 4), 500)

    def test_a_budget_tighter_than_the_floor_is_never_lengthened(self):
        self.assertLessEqual(timing.absent_budget(300, 4), 300)


class BudgetForTests(unittest.TestCase):
    def test_a_resolving_step_keeps_todays_formula(self):
        step = {"op": "tap", "sel": {"label": "a"}}
        self.assertEqual(timing.budget_for(step, {"hits": [120] * 3}), timing.budget_ms([120] * 3))

    def test_a_never_present_step_decays_once_confident(self):
        step = {"op": "tapIfPresent", "sel": {"label": "a"}, "timeoutMs": 1800}
        self.assertIsNone(timing.budget_for(step, {"misses": timing.MISS_CONFIDENCE - 1}))
        self.assertEqual(timing.budget_for(step, {"misses": timing.MISS_CONFIDENCE}), 1080)

    def test_a_step_that_ever_appeared_is_never_decayed(self):
        step = {"op": "tapIfPresent", "sel": {"label": "a"}, "timeoutMs": 1800}
        self.assertIsNone(timing.budget_for(step, {"hits": [412], "misses": 30}))

    def test_a_step_with_no_authored_timeout_is_never_decayed(self):
        step = {"op": "tapIfPresent", "sel": {"label": "a"}}
        self.assertIsNone(timing.budget_for(step, {"misses": 30}))


class ObserveTests(unittest.TestCase):
    def test_a_skipped_group_yields_no_evidence(self):
        steps = [{"op": "group", "steps": [{"op": "tapIfPresent", "sel": {"label": "a"}}]}]
        results = [{"index": 0, "op": "group", "ms": 4, "ok": True, "skipped": True}]
        self.assertEqual(timing.observe(steps, results), ({}, set()))

    def test_a_composite_is_never_counted_absent(self):
        steps = [{"op": "group", "steps": [{"op": "tap", "sel": {"label": "a"}}]}]
        results = [{"index": 0, "op": "group", "ms": 4, "ok": True, "skipped": True,
                    "children": [{"index": 0, "op": "tap", "ms": 40, "ok": True}]}]
        hits, absent = timing.observe(steps, results)
        self.assertEqual(absent, set())

    def test_absent_in_every_cycle_counts(self):
        steps = [{"op": "repeatUntil", "steps": [{"op": "tapIfPresent", "sel": {"label": "Done"}}]}]
        inner = [{"index": 0, "op": "tapIfPresent", "ms": 1800, "ok": True, "skipped": True}] * 2
        results = [{"index": 0, "op": "repeatUntil", "ms": 3700, "ok": True, "children": inner}]
        _, absent = timing.observe(steps, results)
        self.assertEqual(absent, {timing.entry_key([0, 0], steps[0]["steps"][0])})

    def test_present_on_a_later_cycle_is_not_absent(self):
        steps = [{"op": "repeatUntil", "steps": [{"op": "tapIfPresent", "sel": {"label": "Done"}}]}]
        inner = [
            {"index": 0, "op": "tapIfPresent", "ms": 1800, "ok": True, "skipped": True},
            {"index": 0, "op": "tapIfPresent", "ms": 412, "ok": True},
        ]
        results = [{"index": 0, "op": "repeatUntil", "ms": 2300, "ok": True, "children": inner}]
        hits, absent = timing.observe(steps, results)
        key = timing.entry_key([0, 0], steps[0]["steps"][0])
        self.assertEqual(absent, set())
        self.assertEqual(hits[key], 412)

    def test_a_skipped_set_switch_is_not_absence(self):
        steps = [{"op": "setSwitch", "sel": {"label": "Allow Full Access"}, "on": True, "timeoutMs": 5000}]
        results = [{"index": 0, "op": "setSwitch", "ms": 900, "ok": True, "skipped": True,
                    "info": "already=true"}]
        hits, absent = timing.observe(steps, results)
        self.assertEqual(absent, set())

    def test_present_in_a_later_cycle_of_a_nested_group_is_not_absent(self):
        inner = {"op": "tapIfPresent", "sel": {"label": "Done"}}
        steps = [{"op": "repeatUntil", "steps": [{"op": "group", "steps": [inner]}]}]
        first = {"index": 0, "op": "group", "ms": 1900, "ok": True, "children": [
            {"index": 0, "op": "tapIfPresent", "ms": 1800, "ok": True, "skipped": True}]}
        second = {"index": 0, "op": "group", "ms": 500, "ok": True, "children": [
            {"index": 0, "op": "tapIfPresent", "ms": 412, "ok": True}]}
        results = [{"index": 0, "op": "repeatUntil", "ms": 2400, "ok": True, "children": [first, second]}]
        hits, absent = timing.observe(steps, results)
        self.assertEqual(absent, set())
        self.assertEqual(hits[timing.entry_key([0, 0, 0], inner)], 412)

    def test_results_beyond_the_step_count_are_ignored(self):
        steps = [{"op": "tap", "sel": {"label": "a"}}]
        results = [{"index": 0, "op": "tap", "ms": 40, "ok": True},
                   {"index": 1, "op": "assert", "ms": 10, "ok": True}]
        hits, _ = timing.observe(steps, results)
        self.assertEqual(len(hits), 1)

class OwnedObserveTests(unittest.TestCase):
    CALLER = "com.example.Demo/caller"

    def test_present_in_a_later_cycle_of_a_nested_group_is_not_absent(self):
        inner = {"op": "tapIfPresent", "sel": {"label": "Done"}}
        steps = [{"op": "repeatUntil", "steps": [{"op": "group", "steps": [inner]}]}]
        origins = {
            (0,): (self.CALLER, (0,)),
            (0, 0): (self.CALLER, (0, 0)),
            (0, 0, 0): (self.CALLER, (0, 0, 0)),
        }
        first = {"index": 0, "op": "group", "ms": 1900, "ok": True, "children": [
            {"index": 0, "op": "tapIfPresent", "ms": 1800, "ok": True, "skipped": True}]}
        second = {"index": 0, "op": "group", "ms": 500, "ok": True, "children": [
            {"index": 0, "op": "tapIfPresent", "ms": 412, "ok": True}]}
        result = {"children": [
            {"index": 0, "op": "repeatUntil", "ms": 2400, "ok": True, "children": [first, second]}]}
        collected, missing = timing.collect_all(steps, result, origins)
        self.assertEqual(missing, {})
        self.assertEqual(collected[self.CALLER][timing.entry_key([0, 0, 0], inner)], 412)

    def test_a_skipped_set_switch_is_not_absence(self):
        step = {"op": "setSwitch", "sel": {"label": "Allow Full Access"}, "on": True, "timeoutMs": 5000}
        origins = {(0,): (self.CALLER, (0,))}
        result = {"children": [{"index": 0, "op": "setSwitch", "ms": 900, "ok": True, "skipped": True}]}
        _, missing = timing.collect_all([step], result, origins)
        self.assertEqual(missing, {})

    def test_collect_still_returns_only_the_samples(self):
        step = {"op": "tap", "sel": {"label": "x"}}
        origins = {(0,): (self.CALLER, (0,))}
        result = {"children": [{"index": 0, "op": "tap", "ms": 33, "ok": True}]}
        self.assertEqual(timing.collect([step], result, origins),
                         {self.CALLER: {timing.entry_key([0], step): 33}})


def records_to_group(record):
    from uiflow import records

    return records.to_group(record)


if __name__ == "__main__":
    unittest.main()
