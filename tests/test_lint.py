import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import journal, lint
from uiflow.commands import record as record_command

SETTINGS = "com.apple.Preferences"
GENERAL = {"app": SETTINGS, "type": "button", "id": "com.apple.settings.general"}
FULL_ACCESS = {"app": SETTINGS, "type": "switch", "label": "Full Access"}
SWIPE_DOWN = {"op": "swipe", "app": SETTINGS, "direction": "down"}
SWIPE_UP = {"op": "swipe", "app": SETTINGS, "direction": "up"}
SCROLL_GENERAL = {"op": "scrollTo", "sel": GENERAL, "maxSwipes": 4}
FIXED = {"op": "scrollTo", "sel": GENERAL, "direction": "down", "maxSwipes": 4}
OTHER_APP = "com.host.App"
CAVEAT = ("scrollTo gives up after two swipes that do not change the match count")


def rules(steps, **probes):
    return [finding.rule for finding in lint.check(steps, **probes)]


class FlagshipTests(unittest.TestCase):
    """Three swipes to the top of Settings cost 3.2s of a 15.6s flow and could never fail."""

    def test_the_run_of_three_before_the_scroll_is_flagged_once(self):
        found = lint.check([dict(SWIPE_DOWN), dict(SWIPE_DOWN), dict(SWIPE_DOWN),
                            dict(SCROLL_GENERAL)])
        self.assertEqual([finding.rule for finding in found], ["redundant-swipe-before-scrollTo"])
        self.assertEqual(found[0].steps, ("0", "1", "2", "3"))

    def test_the_single_swipe_before_the_scroll_is_flagged_too(self):
        found = lint.check([dict(SWIPE_DOWN), dict(SCROLL_GENERAL)])
        self.assertEqual([finding.rule for finding in found], ["redundant-swipe-before-scrollTo"])
        self.assertEqual(found[0].steps, ("0", "1"))

    def test_the_message_names_the_exact_corrected_shape(self):
        for steps in ([dict(SWIPE_DOWN), dict(SCROLL_GENERAL)],
                      [dict(SWIPE_DOWN), dict(SWIPE_DOWN), dict(SWIPE_DOWN), dict(SCROLL_GENERAL)]):
            message = lint.check(steps)[0].message
            self.assertIn("delete", message)
            self.assertIn('"direction": "down"', message)
            self.assertIn("com.apple.settings.general", message)
            self.assertIn("scrollTo at step %d" % (len(steps) - 1), message)
            self.assertIn("Replay the flow after this edit: " + CAVEAT, message)

    def test_the_corrected_record_produces_no_findings(self):
        self.assertEqual(lint.check([dict(FIXED)]), [])

    def test_an_ordinary_flow_produces_no_findings(self):
        clean = [
            {"op": "launch", "app": SETTINGS},
            {"op": "tap", "sel": GENERAL},
            {"op": "scrollTo", "sel": FULL_ACCESS},
            {"op": "setSwitch", "sel": FULL_ACCESS, "on": True},
        ]
        self.assertEqual(lint.check(clean), [])


class RedundantSwipeTests(unittest.TestCase):
    def test_a_same_direction_swipe_is_told_only_to_be_deleted(self):
        message = lint.check([{"op": "swipe", "app": SETTINGS}, dict(SCROLL_GENERAL)])[0].message
        self.assertIn("swipes up immediately before", message)
        self.assertIn("delete the swipe", message)
        self.assertNotIn('"direction"', message)

    def test_an_opposite_run_is_told_to_delete_and_to_add_the_direction(self):
        message = lint.check([dict(SWIPE_DOWN), dict(SWIPE_DOWN), dict(SCROLL_GENERAL)])[0].message
        self.assertIn("delete the 2 swipes", message)
        self.assertIn('add "direction": "down" to the scrollTo at step 2', message)

    def test_the_opposite_branch_does_not_claim_the_target_is_unreachable(self):
        """scrollTo probes before its first swipe, so a target already on screen resolves at once."""
        message = lint.check([dict(SWIPE_DOWN), dict(SWIPE_DOWN), dict(SCROLL_GENERAL)])[0].message
        self.assertIn("swipes up instead, away from where they leave the screen", message)
        self.assertNotIn("can never reach", message)
        self.assertIn(CAVEAT, message)

    def test_a_swipe_on_another_app_is_never_paired_with_the_scroll(self):
        swipe = {"op": "swipe", "app": OTHER_APP, "direction": "down"}
        self.assertEqual(lint.check([dict(swipe), dict(SCROLL_GENERAL)]), [])

    def test_the_run_stops_at_a_swipe_belonging_to_another_app(self):
        found = lint.check([{"op": "swipe", "app": OTHER_APP, "direction": "down"},
                            dict(SWIPE_DOWN), dict(SCROLL_GENERAL)])
        self.assertEqual([finding.rule for finding in found], ["redundant-swipe-before-scrollTo"])
        self.assertEqual(found[0].steps, ("1", "2"))

    def test_an_app_less_swipe_pairs_with_a_scroll_whose_selector_names_one(self):
        found = lint.check([{"op": "swipe", "direction": "down"}, dict(SCROLL_GENERAL)])
        self.assertEqual([finding.rule for finding in found], ["redundant-swipe-before-scrollTo"])

    def test_an_empty_sel_is_still_a_resolve_then_swipe_gesture(self):
        swipe = {"op": "swipe", "app": SETTINGS, "direction": "down", "sel": {}}
        self.assertEqual(lint.check([dict(swipe), dict(SCROLL_GENERAL)]), [])

    def test_a_horizontal_run_before_a_scroll_to_is_never_flagged(self):
        for direction in ("left", "right"):
            swipe = {"op": "swipe", "app": SETTINGS, "direction": direction}
            self.assertEqual(lint.check([dict(swipe), dict(swipe), dict(SCROLL_GENERAL)]), [])

    def test_a_sel_carrying_swipe_targets_another_container_and_is_never_flagged(self):
        carousel = {"op": "swipe", "sel": FULL_ACCESS, "direction": "down"}
        self.assertEqual(lint.check([carousel, dict(SCROLL_GENERAL)]), [])

    def test_the_run_stops_at_a_swipe_of_a_different_direction(self):
        found = lint.check([dict(SWIPE_UP), dict(SWIPE_DOWN), dict(SCROLL_GENERAL)])
        self.assertEqual([finding.rule for finding in found], ["redundant-swipe-before-scrollTo"])
        self.assertEqual(found[0].steps, ("1", "2"))


class RepeatedSwipeTests(unittest.TestCase):
    def test_a_vertical_run_with_no_scroll_after_it_is_flagged(self):
        found = lint.check([dict(SWIPE_DOWN), dict(SWIPE_DOWN), {"op": "tap", "sel": GENERAL}])
        self.assertEqual([finding.rule for finding in found], ["repeated-swipe"])
        self.assertEqual(found[0].steps, ("0", "1"))
        self.assertIn('"direction": "down"', found[0].message)
        self.assertIn("com.apple.settings.general", found[0].message)

    def test_a_horizontal_run_is_never_flagged(self):
        swipe = {"op": "swipe", "app": SETTINGS, "direction": "left"}
        self.assertEqual(lint.check([dict(swipe), dict(swipe), {"op": "tap", "sel": GENERAL}]), [])

    def test_a_run_of_sel_carrying_swipes_is_never_flagged(self):
        swipe = {"op": "swipe", "sel": FULL_ACCESS, "direction": "down"}
        self.assertEqual(lint.check([dict(swipe), dict(swipe), {"op": "tap", "sel": GENERAL}]), [])

    def test_the_run_needs_an_identical_payload(self):
        differing = [dict(SWIPE_DOWN), dict(SWIPE_UP), {"op": "tap", "sel": GENERAL}]
        self.assertEqual(lint.check(differing), [])

    def test_a_differing_timeout_alone_still_counts_as_identical(self):
        pair = [dict(SWIPE_DOWN), dict(SWIPE_DOWN, timeoutMs=9000), {"op": "tap", "sel": GENERAL}]
        self.assertEqual(rules(pair), ["repeated-swipe"])

    def test_the_swipes_must_be_consecutive(self):
        split = [dict(SWIPE_DOWN), {"op": "tap", "sel": GENERAL}, dict(SWIPE_DOWN),
                 {"op": "tap", "sel": GENERAL}]
        self.assertEqual(lint.check(split), [])

    def test_the_message_names_the_immediately_following_selector_and_the_caveat(self):
        message = lint.check([dict(SWIPE_DOWN), dict(SWIPE_DOWN),
                              {"op": "tap", "sel": GENERAL}])[0].message
        self.assertIn("steps 0-1 are 2 identical down swipes", message)
        self.assertIn("the next resolving step names (id=com.apple.settings.general)", message)
        self.assertIn("Replay the flow after this edit: " + CAVEAT, message)

    def test_a_run_that_ends_the_record_names_no_target_and_is_not_flagged(self):
        self.assertEqual(lint.check([{"op": "tap", "sel": GENERAL}, dict(SWIPE_DOWN),
                                     dict(SWIPE_DOWN)]), [])

    def test_a_run_followed_by_a_selector_less_step_is_not_flagged(self):
        self.assertEqual(lint.check([dict(SWIPE_DOWN), dict(SWIPE_DOWN), {"op": "screenshot"}]), [])

    def test_the_target_is_never_taken_from_beyond_the_next_step(self):
        steps = [dict(SWIPE_DOWN), dict(SWIPE_DOWN), {"op": "screenshot"},
                 {"op": "tap", "sel": {"app": SETTINGS, "label": "NextScreen"}}]
        self.assertEqual(lint.check(steps), [])

    def test_a_target_in_another_app_is_no_target(self):
        steps = [dict(SWIPE_DOWN), dict(SWIPE_DOWN),
                 {"op": "tap", "sel": {"app": OTHER_APP, "label": "Elsewhere"}}]
        self.assertEqual(lint.check(steps), [])

    def test_a_run_of_empty_sel_swipes_is_never_flagged(self):
        swipe = {"op": "swipe", "app": SETTINGS, "direction": "down", "sel": {}}
        self.assertEqual(lint.check([dict(swipe), dict(swipe), {"op": "tap", "sel": GENERAL}]), [])


class FalsePositiveTests(unittest.TestCase):
    """Every shape here is correctly written; a lint that fires on one trains the reader to ignore it."""

    def test_a_swipe_closing_a_repeat_until_body_is_not_flagged(self):
        steps = [{"op": "repeatUntil", "until": [{"op": "assert", "sel": GENERAL}],
                  "steps": [{"op": "tap", "sel": FULL_ACCESS}, dict(SWIPE_DOWN)]}]
        self.assertEqual(lint.check(steps), [])

    def test_a_groups_trailing_swipe_is_not_flagged(self):
        steps = [{"op": "group", "name": "reveal", "steps": [dict(SWIPE_DOWN)],
                  "verifies": [{"op": "assert", "sel": GENERAL}]}]
        self.assertEqual(lint.check(steps), [])

    def test_a_trailing_swipe_at_the_top_level_is_not_flagged(self):
        self.assertEqual(lint.check([{"op": "tap", "sel": GENERAL}, dict(SWIPE_DOWN)]), [])

    def test_a_swipe_whose_next_step_resolves_nothing_is_not_flagged(self):
        self.assertEqual(lint.check([dict(SWIPE_DOWN), {"op": "screenshot"}]), [])

    def test_a_run_closing_a_repeat_until_body_is_not_flagged(self):
        steps = [{"op": "repeatUntil", "until": [{"op": "assert", "sel": GENERAL}],
                  "steps": [{"op": "tap", "sel": FULL_ACCESS}, dict(SWIPE_DOWN), dict(SWIPE_DOWN)]}]
        self.assertEqual(lint.check(steps), [])

    def test_a_groups_trailing_run_is_not_flagged(self):
        steps = [{"op": "group", "name": "reveal", "steps": [dict(SWIPE_DOWN), dict(SWIPE_DOWN)],
                  "verifies": [{"op": "assert", "sel": GENERAL}]}]
        self.assertEqual(lint.check(steps), [])

    def test_a_trailing_run_at_the_top_level_is_not_flagged(self):
        self.assertEqual(lint.check([{"op": "tap", "sel": GENERAL}, dict(SWIPE_DOWN),
                                     dict(SWIPE_DOWN)]), [])

    def test_a_run_whose_next_step_resolves_nothing_is_not_flagged(self):
        self.assertEqual(lint.check([dict(SWIPE_DOWN), dict(SWIPE_DOWN), {"op": "screenshot"}]), [])


class ProbeListTests(unittest.TestCase):
    def test_a_bare_sleep_in_a_groups_reassert_is_flagged_and_says_where(self):
        steps = [{"op": "group", "name": "type", "steps": [{"op": "tap", "sel": GENERAL}],
                  "reassert": [{"op": "sleep", "ms": 400}, {"op": "assert", "sel": FULL_ACCESS}]}]
        found = lint.check(steps)
        self.assertEqual([finding.rule for finding in found], ["bare-sleep"])
        self.assertEqual(found[0].steps, ("0.reassert.0",))
        self.assertIn("0.reassert.0", found[0].message)
        self.assertIn("focus-moving step", found[0].message)

    def test_a_bare_sleep_in_satisfied_when_is_flagged(self):
        found = lint.check([{"op": "tap", "sel": GENERAL}],
                           satisfied_when=[{"op": "sleep", "ms": 900}])
        self.assertEqual([finding.rule for finding in found], ["bare-sleep"])
        self.assertEqual(found[0].steps, ("satisfiedWhen.0",))
        self.assertIn("900", found[0].message)
        self.assertIn("waitFor", found[0].message)

    def test_a_bare_sleep_in_verifies_is_flagged(self):
        found = lint.check([{"op": "tap", "sel": GENERAL}], verifies=[{"op": "sleep", "ms": 250}])
        self.assertEqual(found[0].steps, ("verifies.0",))

    def test_a_groups_own_satisfied_when_is_walked(self):
        steps = [{"op": "group", "name": "open", "satisfiedWhen": [{"op": "sleep", "ms": 100}],
                  "steps": [{"op": "tap", "sel": GENERAL}]}]
        self.assertEqual(lint.check(steps)[0].steps, ("0.satisfiedWhen.0",))

    def test_a_repeat_untils_until_is_walked(self):
        steps = [{"op": "repeatUntil", "until": [{"op": "sleep", "ms": 100}],
                  "steps": [{"op": "tap", "sel": GENERAL}]}]
        self.assertEqual(lint.check(steps)[0].steps, ("0.until.0",))

    def test_steps_paths_read_exactly_as_timing_keys_do(self):
        steps = [{"op": "group", "name": "scroll",
                  "steps": [dict(SWIPE_DOWN), dict(SWIPE_DOWN), {"op": "tap", "sel": GENERAL}]}]
        self.assertEqual(lint.check(steps)[0].steps, ("0.0", "0.1"))

    def test_findings_are_ordered_by_path_with_the_probe_lists_last(self):
        found = lint.check(
            [{"op": "sleep", "ms": 100}, {"op": "group", "steps": [{"op": "sleep", "ms": 200}]},
             {"op": "sleep", "ms": 300}],
            satisfied_when=[{"op": "sleep", "ms": 400}], verifies=[{"op": "sleep", "ms": 500}])
        self.assertEqual([finding.steps for finding in found],
                         [("0",), ("1.0",), ("2",), ("satisfiedWhen.0",), ("verifies.0",)])


class DeduplicationTests(unittest.TestCase):
    def test_the_flagship_run_is_reported_once_not_by_both_swipe_rules(self):
        for count in (1, 2, 3):
            steps = [dict(SWIPE_DOWN) for _ in range(count)] + [dict(SCROLL_GENERAL)]
            self.assertEqual(rules(steps), ["redundant-swipe-before-scrollTo"])

    def test_a_corrective_swipe_hides_the_later_scrolls_target_from_the_run(self):
        """The up-run's next step is the corrective swipe, so nothing names a target for it."""
        found = lint.check([dict(SWIPE_UP), dict(SWIPE_UP), dict(SWIPE_DOWN),
                            dict(SCROLL_GENERAL)])
        self.assertEqual([finding.rule for finding in found], ["redundant-swipe-before-scrollTo"])
        self.assertEqual(found[0].steps, ("2", "3"))
        self.assertIn("step 2 swipes down immediately before the scrollTo at step 3", found[0].message)
        self.assertNotIn("steps 0-1", found[0].message)


class MalformedInputTests(unittest.TestCase):
    """A lint must never be the reason a hard-won recording is lost."""

    def test_a_child_that_is_a_dict_rather_than_a_list_is_skipped(self):
        steps = [{"op": "sleep", "ms": 100}, {"op": "group", "steps": {"op": "sleep", "ms": 200}}]
        self.assertEqual([finding.steps for finding in lint.check(steps)], [("0",)])

    def test_a_none_step_is_skipped(self):
        self.assertEqual([finding.steps for finding in lint.check([None, {"op": "sleep", "ms": 100}])],
                         [("1",)])

    def test_a_non_dict_step_is_skipped(self):
        self.assertEqual(lint.check(["swipe", 7, {"op": "tap", "sel": GENERAL}]), [])

    def test_a_non_list_top_level_is_skipped(self):
        self.assertEqual(lint.check({"op": "sleep", "ms": 100}), [])
        self.assertEqual(lint.check(None), [])


class JournalledInputTests(unittest.TestCase):
    """What `record save` actually lints is journal.to_steps output, not a hand-written shape."""

    ENTRIES = [
        {"ts": 1, "seq": 1, "ok": True, "op": "sleep", "ms": 700},
        {"ts": 2, "seq": 2, "ok": True, "step": {"op": "swipe", "app": SETTINGS, "direction": "down"}},
        {"ts": 3, "seq": 3, "ok": True, "step": {"op": "swipe", "app": SETTINGS, "direction": "down"}},
        {"ts": 4, "seq": 4, "ok": True, "step": {"op": "scrollTo", "sel": GENERAL, "maxSwipes": 4}},
        {"ts": 5, "seq": 5, "ok": True, "step": {"op": "tap", "sel": GENERAL}},
    ]

    def test_the_journalled_swipes_survive_and_are_flagged_but_the_sleep_is_scaffolding(self):
        steps = journal.to_steps(self.ENTRIES)
        self.assertEqual([step["op"] for step in steps], ["swipe", "swipe", "scrollTo", "tap"])
        found = lint.check(steps)
        self.assertEqual([finding.rule for finding in found], ["redundant-swipe-before-scrollTo"])
        self.assertEqual(found[0].steps, ("0", "1", "2"))
        self.assertIn('add "direction": "down" to the scrollTo at step 2', found[0].message)

    def test_a_sleep_nested_in_a_journalled_group_still_reaches_the_lint(self):
        entries = [{"ts": 1, "seq": 1, "ok": True,
                    "step": {"op": "group", "name": "open", "steps": [{"op": "sleep", "ms": 500}]}}]
        self.assertEqual(rules(journal.to_steps(entries)), ["bare-sleep"])


class FakeOracle:
    lang = "ko-US"
    REVERSE = {"일반": ["General"]}

    def originals(self, label):
        return list(self.REVERSE.get(label) or [])

    def canonical(self, label):
        found = self.originals(label)
        return found[0] if len(found) == 1 else None

    def knows_english(self, label):
        return label == "General"

    def knows_localized(self, label):
        return label in self.REVERSE


class BuildRecordTests(unittest.TestCase):
    def build(self, steps, warn, **kwargs):
        return record_command.build_record(
            app=SETTINGS, name="scroll-to-general", steps=steps,
            verifies_why="because", warn=warn, **kwargs)

    def test_the_finding_reaches_the_warn_channel(self):
        warnings = []
        built = self.build([dict(SWIPE_DOWN), dict(SWIPE_DOWN), dict(SWIPE_DOWN),
                            dict(SCROLL_GENERAL)], warnings.append)
        named = [line for line in warnings if "immediately before the scrollTo" in line]
        self.assertEqual(len(named), 1)
        self.assertTrue(named[0].startswith("WARNING: "))
        self.assertIn("com.apple.settings.general", named[0])
        self.assertEqual(len(built["steps"]), 4)

    def test_a_lint_finding_never_refuses_the_save(self):
        built = self.build([{"op": "sleep", "ms": 500}], lambda line: None)
        self.assertEqual(built["steps"][0]["op"], "sleep")

    def test_a_malformed_child_never_refuses_the_save(self):
        built = self.build([{"op": "group", "steps": {"op": "sleep", "ms": 200}}],
                           lambda line: None)
        self.assertEqual(len(built["steps"]), 1)

    def test_the_probe_lists_are_linted_too(self):
        warnings = []
        self.build([{"op": "tap", "sel": GENERAL}], warnings.append,
                   satisfied_when=[{"op": "sleep", "ms": 900}],
                   verifies=[{"op": "assert", "sel": GENERAL}])
        named = [line for line in warnings if "satisfiedWhen.0" in line]
        self.assertEqual(len(named), 1)
        self.assertIn("900", named[0])

    def test_a_clean_flow_draws_no_lint_warning(self):
        warnings = []
        self.build([{"op": "tap", "sel": GENERAL}], warnings.append)
        self.assertFalse([line for line in warnings if "swipe" in line or "sleep" in line])

    def test_what_is_linted_is_what_gets_written(self):
        """Canonicalization runs first, so the message must name the English label, not the observed one."""
        warnings = []
        built = record_command.build_record(
            app=SETTINGS, name="scroll-to-general", verifies_why="because", warn=warnings.append,
            steps=[{"op": "swipe", "app": SETTINGS},
                   {"op": "scrollTo", "sel": {"app": SETTINGS, "type": "button", "label": "일반"}}],
            oracle=FakeOracle())
        self.assertEqual(built["steps"][1]["sel"]["label"], "General")
        named = [line for line in warnings if "swipes up immediately before" in line]
        self.assertEqual(len(named), 1)
        self.assertIn("label=General", named[0])


BARE_TAP_AT = {"op": "tapAt", "dx": 0.3847, "dy": 0.4987}
ANCHORED_TAP_AT = {"op": "tapAt", "sel": dict(GENERAL), "dx": 0.5, "dy": 0.5}


class DeviceShapeTests(unittest.TestCase):
    """A bare tapAt offsets inside the app WINDOW, so it aims elsewhere at another aspect ratio."""

    def test_a_bare_tap_at_is_flagged(self):
        found = lint.check([dict(BARE_TAP_AT)])
        self.assertEqual([finding.rule for finding in found], ["device-shaped-tapAt"])
        self.assertEqual(found[0].steps, ("0",))

    def test_a_tap_at_carrying_a_sel_is_not_flagged(self):
        self.assertEqual(rules([dict(ANCHORED_TAP_AT)]), [])

    def test_the_message_says_what_to_do(self):
        message = lint.check([dict(BARE_TAP_AT)])[0].message
        self.assertIn("sel", message)
        self.assertIn("window", message.lower())

    def test_a_nested_bare_tap_at_is_found_under_its_group_path(self):
        found = lint.check([{"op": "group", "name": "g", "steps": [dict(BARE_TAP_AT)]}])
        self.assertEqual(found[0].steps, ("0.0",))

    def test_a_bare_tap_at_in_a_probe_list_is_flagged_under_its_prefix(self):
        found = lint.check([], satisfied_when=[dict(BARE_TAP_AT)])
        self.assertEqual(found[0].steps, ("satisfiedWhen.0",))


class ShapeDependenceTests(unittest.TestCase):
    def test_a_selector_only_record_is_device_independent(self):
        self.assertEqual(lint.shape_dependent([dict(SCROLL_GENERAL), dict(ANCHORED_TAP_AT)]), ())

    def test_a_bare_tap_at_makes_a_record_shape_dependent(self):
        self.assertEqual(lint.shape_dependent([dict(SCROLL_GENERAL), dict(BARE_TAP_AT)]), ("1",))

    def test_probe_lists_count_too(self):
        self.assertEqual(lint.shape_dependent([], verifies=[dict(BARE_TAP_AT)]), ("verifies.0",))

    def test_it_does_not_report_other_rules(self):
        self.assertEqual(lint.shape_dependent([dict(SWIPE_DOWN), dict(SCROLL_GENERAL)]), ())


if __name__ == "__main__":
    unittest.main()
