import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import compose, localize, timing

TABLE = {"General": ["일반"], "Keyboard": ["키보드"], "Allow Full Access": ["전체 접근 허용"],
         "Keyboards": ["키보드"]}
REVERSE = {"일반": "General", "키보드": "Keyboard", "전체 접근 허용": "Allow Full Access",
           "Clavier": "Keyboard"}


def translate(label):
    return list(TABLE.get(label) or [])


AMBIGUOUS = {"확인": ("Acknowledge", "Confirm", "OK")}


class FakeOracle:
    lang = "ko-US"

    def translate(self, label):
        return translate(label)

    def originals(self, label):
        if label in AMBIGUOUS:
            return list(AMBIGUOUS[label])
        found = REVERSE.get(label)
        return [found] if found else []

    def canonical(self, label):
        found = self.originals(label)
        return found[0] if len(found) == 1 else None

    def knows_english(self, label):
        return label in TABLE

    def knows_localized(self, label):
        return label in REVERSE or label in AMBIGUOUS


class StepKeysTests(unittest.TestCase):
    def test_the_step_key_set_lives_in_one_place(self):
        self.assertEqual(compose.STEP_KEYS, ("steps",) + compose.PROBE_KEYS)


class ExpandTests(unittest.TestCase):
    def test_a_label_becomes_an_any_of_that_still_contains_the_english(self):
        selector, missing, seen = localize.expand({"type": "button", "label": "General"}, translate)
        self.assertEqual(selector, {"type": "button", "labelAnyOf": ["General", "일반"]})
        self.assertEqual((missing, seen), ([], 1))

    def test_label_contains_becomes_its_own_any_of(self):
        selector, _, _ = localize.expand({"type": "button", "labelContains": "Keyboard"}, translate)
        self.assertEqual(selector["labelContainsAnyOf"], ["Keyboard", "키보드"])
        self.assertNotIn("labelContains", selector)

    def test_the_trimmed_fallback_never_widens_the_fragment_it_asked_for(self):
        """`Keyboards,` gaining `Keyboards` drops the comma that disambiguated the row."""
        selector, missing, _ = localize.expand(
            {"labelContains": "Keyboards,"},
            lambda label: ["Keyboards"] if label == "Keyboards" else [])
        self.assertEqual(selector, {"labelContains": "Keyboards,"})
        self.assertEqual(missing, ["Keyboards,"])

    def test_a_fragment_with_trailing_punctuation_still_translates(self):
        """Accessibility labels compose: the real record targets `Keyboards,` which is in no catalogue."""
        selector, missing, _ = localize.expand(
            {"type": "button", "labelContains": "Keyboards,"}, translate)
        self.assertEqual(selector["labelContainsAnyOf"], ["Keyboards,", "키보드"])
        self.assertEqual(missing, [])

    def test_an_untranslatable_label_is_left_alone_and_reported(self):
        selector, missing, seen = localize.expand({"type": "button", "label": "Nope"}, translate)
        self.assertEqual(selector, {"type": "button", "label": "Nope"})
        self.assertEqual((missing, seen), (["Nope"], 1))

    def test_an_id_selector_is_untouched_and_counts_no_label(self):
        selector = {"type": "button", "id": "com.apple.settings.general"}
        self.assertEqual(localize.expand(selector, translate), (selector, [], 0))

    def test_a_descendant_selector_is_expanded_too(self):
        selector, _, seen = localize.expand(
            {"type": "switch", "label": "Allow Full Access",
             "descendant": {"type": "switch", "label": "General"}}, translate)
        self.assertEqual(selector["descendant"]["labelAnyOf"], ["General", "일반"])
        self.assertEqual(seen, 2)


class LocalizeTests(unittest.TestCase):
    def steps(self):
        return [
            {"op": "group", "name": "walk",
             "satisfiedWhen": [{"op": "assert", "sel": {"type": "switch", "label": "Allow Full Access"}}],
             "steps": [
                 {"op": "tap", "sel": {"type": "button", "labelContains": "General"}},
                 {"op": "firstMatch", "sels": [{"type": "button", "label": "Keyboard"},
                                               {"type": "button", "label": "Nope"}]},
                 {"op": "repeatUntil", "steps": [{"op": "swipe", "direction": "up"}],
                  "until": [{"op": "assert", "sel": {"type": "button", "label": "Keyboard"}}]},
             ],
             "verifies": [{"op": "assert", "sel": {"type": "button", "label": "Nope"}}]},
        ]

    def test_every_probe_key_and_nesting_level_is_reached(self):
        localized, missing, seen = localize.localize(self.steps(), translate)
        group = localized[0]
        self.assertEqual(group["satisfiedWhen"][0]["sel"]["labelAnyOf"][1], "전체 접근 허용")
        self.assertEqual(group["steps"][0]["sel"]["labelContainsAnyOf"][1], "일반")
        self.assertEqual(group["steps"][2]["until"][0]["sel"]["labelAnyOf"][1], "키보드")
        self.assertEqual(missing, ["Nope"])
        self.assertEqual(seen, 6)

    def test_a_first_match_step_has_every_selector_in_its_list_expanded(self):
        localized, _, _ = localize.localize(self.steps(), translate)
        sels = localized[0]["steps"][1]["sels"]
        self.assertEqual(sels[0]["labelAnyOf"], ["Keyboard", "키보드"])
        self.assertEqual(sels[1]["label"], "Nope")

    def test_the_input_batch_is_not_mutated(self):
        original = self.steps()
        before = copy.deepcopy(original)
        localize.localize(original, translate)
        self.assertEqual(original, before)

    def test_a_step_with_no_selector_survives(self):
        steps = [{"op": "launch", "app": "com.apple.Preferences"}, {"op": "sleep", "ms": 10}]
        self.assertEqual(localize.localize(steps, translate), (steps, [], 0))

    def test_localizing_after_fingerprinting_leaves_the_canonical_key_intact(self):
        """The whole reason localization runs last: a ko run inherits the en record's budgets."""
        step = {"op": "tap", "sel": {"type": "button", "label": "General"}}
        before = timing.entry_key([0], step)
        localized, _, _ = localize.localize([step], translate)
        self.assertNotEqual(timing.entry_key([0], localized[0]), before)
        self.assertEqual(timing.entry_key([0], step), before)


class CanonicalizeTests(unittest.TestCase):
    def test_a_localized_label_is_mapped_back_to_english(self):
        selector, refused = localize.canonicalize({"type": "button", "label": "일반"}, FakeOracle())
        self.assertEqual(selector, {"type": "button", "label": "General"})
        self.assertEqual(refused, [])

    def test_a_latin_script_localized_label_is_mapped_too(self):
        """isascii() would have waved `Clavier` straight into a canonical record."""
        selector, refused = localize.canonicalize({"type": "button", "label": "Clavier"}, FakeOracle())
        self.assertEqual(selector["label"], "Keyboard")
        self.assertEqual(refused, [])

    def test_a_known_english_label_is_left_alone(self):
        selector = {"type": "button", "label": "General"}
        self.assertEqual(localize.canonicalize(selector, FakeOracle()), (selector, []))

    def test_a_string_the_store_never_localized_is_left_exactly_as_it_is(self):
        """`Bluetooth` is spelled the same in ko, so `iter_pairs` drops it and it is no `pair.en`.

        Asking `knows_english` there answered False and threw the repair away.
        """
        for label in ("Bluetooth", "설정"):
            selector, ambiguous = localize.canonicalize(
                {"type": "button", "label": label}, FakeOracle())
            self.assertEqual(selector, {"type": "button", "label": label})
            self.assertEqual(ambiguous, [])

    def test_a_localized_string_with_several_english_originals_is_refused_not_guessed(self):
        selector, ambiguous = localize.canonicalize({"type": "button", "label": "확인"}, FakeOracle())
        self.assertEqual(selector["label"], "확인")
        self.assertEqual(ambiguous, [("확인", ("Acknowledge", "Confirm", "OK"))])

    def test_an_any_of_collapses_even_when_its_english_is_no_catalogue_source(self):
        """`labelAnyOf` is built english-first by `expand`; a scan for a known en discarded it."""
        selector, ambiguous = localize.canonicalize(
            {"type": "button", "labelContainsAnyOf": ["Keyboards,", "키보드"]}, FakeOracle())
        self.assertEqual(selector, {"type": "button", "labelContains": "Keyboards,"})
        self.assertEqual(ambiguous, [])

    def test_a_carried_english_fragment_under_a_descendant_survives(self):
        selector, ambiguous = localize.canonicalize(
            {"type": "cell", "id": "com.apple.settings.general",
             "descendant": {"labelContains": "Keyboards,"}}, FakeOracle())
        self.assertEqual(selector["descendant"], {"labelContains": "Keyboards,"})
        self.assertEqual(ambiguous, [])

    def test_an_any_of_collapses_back_to_its_english_element(self):
        """The driver journals the SENT step, so a save after a localized run sees labelAnyOf."""
        selector, refused = localize.canonicalize(
            {"type": "button", "labelAnyOf": ["General", "일반"]}, FakeOracle())
        self.assertEqual(selector, {"type": "button", "label": "General"})
        self.assertEqual(refused, [])

    def test_a_contains_any_of_collapses_too(self):
        selector, _ = localize.canonicalize(
            {"type": "button", "labelContainsAnyOf": ["Keyboards,", "키보드"]}, FakeOracle())
        self.assertEqual(selector, {"type": "button", "labelContains": "Keyboards,"})

    def test_without_an_oracle_nothing_moves(self):
        selector = {"type": "button", "label": "일반"}
        self.assertEqual(localize.canonicalize(selector, None), (selector, []))

    def test_a_descendant_selector_is_canonicalized_as_well(self):
        selector, _ = localize.canonicalize(
            {"type": "switch", "label": "전체 접근 허용",
             "descendant": {"type": "switch", "label": "일반"}}, FakeOracle())
        self.assertEqual(selector["descendant"]["label"], "General")


if __name__ == "__main__":
    unittest.main()
