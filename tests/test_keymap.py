import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from uiflow import keymap

APP = "com.example.Demo"


def element(kind, label, frame):
    return {"type": kind, "label": label, "frame": frame, "app": APP}


SNAPSHOT = [
    element("staticText", "Q", [3.0, 90.8, 34.2, 44.3]),
    element("staticText", "G", [183.9, 90.8, 34.2, 44.3]),
    element("staticText", "H", [224.1, 147.0, 34.2, 44.3]),
    element("staticText", "Slyder δ", [16.0, 119.7, 128.0, 40.7]),
    element("button", "Next keyboard", [8.5, 797.4, 68.1, 68.7]),
    {"type": "textField", "frame": [54.0, 418.3, 322.0, 44.0], "app": APP},
]


class SnapshotReadingTests(unittest.TestCase):
    def test_keeps_only_single_character_labels(self):
        self.assertEqual(sorted(keymap.single_char_keys(SNAPSHOT)), ["G", "H", "Q"])

    def test_returns_frame_centres(self):
        centre = keymap.single_char_keys(SNAPSHOT)["Q"]
        self.assertAlmostEqual(centre[0], 20.1)
        self.assertAlmostEqual(centre[1], 112.95)

    def test_ignores_other_element_types(self):
        self.assertEqual(keymap.single_char_keys(SNAPSHOT, element_type="button"), {})

    def test_finds_the_text_field_centre(self):
        point = keymap.text_field_point(SNAPSHOT)
        self.assertAlmostEqual(point[0], 215.0)
        self.assertAlmostEqual(point[1], 440.3)


class OffsetSearchTests(unittest.TestCase):
    def test_zero_is_tried_first_when_the_key_is_already_low_on_screen(self):
        self.assertEqual(keymap.candidate_offsets(601.35, 874.0)[0], 0.0)

    def test_zero_is_not_probed_when_it_would_tap_the_app_instead(self):
        """A key sitting high on screen at offset 0 is not a key — it is the host app's own UI, and
        tapping one opened a dialog that wrecked the rest of the search."""
        self.assertNotIn(0.0, keymap.candidate_offsets(112.95, 874.0))

    def test_never_probes_above_the_halfway_line(self):
        for offset in keymap.candidate_offsets(112.95, 874.0):
            self.assertGreaterEqual(112.95 + offset, 437.0)

    def test_searches_upward_from_the_bottom(self):
        offsets = keymap.candidate_offsets(112.95, 874.0)
        self.assertGreater(offsets[1], offsets[2])
        self.assertLessEqual(offsets[1], 874.0 - 112.95 + keymap.GRID_STEP)

    def test_probes_at_most_one_step_past_the_measured_bottom(self):
        """The snapshot can under-report the screen height, so the search deliberately overshoots
        by one step rather than stopping short of a real offset."""
        for offset in keymap.candidate_offsets(112.95, 874.0):
            self.assertLessEqual(112.95 + offset, 874.0 + keymap.GRID_STEP)

    def test_step_is_finer_than_a_key_is_tall(self):
        offsets = keymap.candidate_offsets(112.95, 874.0)
        self.assertLessEqual(offsets[1] - offsets[2], 44.3)


class ProbeChoiceTests(unittest.TestCase):
    def test_prefers_a_key_surrounded_by_letters(self):
        self.assertEqual(keymap.pick_probe({"Q": [1, 1], "G": [2, 2], "A": [3, 3]}), "G")

    def test_falls_back_when_no_preferred_key_exists(self):
        self.assertIn(keymap.pick_probe({"X": [1, 1], "Y": [2, 2]}), ("X", "Y"))

    def test_returns_none_for_an_empty_map(self):
        self.assertIsNone(keymap.pick_probe({}))


class FakeProber:
    """Types whichever key actually sits under the probed point — so a wrong offset returns a
    NEIGHBOURING key's character, not silence, which is what a live screen does and what the search
    has to keep going through."""

    def __init__(self, keys, true_offset):
        self.keys = keys
        self.true_offset = true_offset
        self.probes = []

    def clear(self):
        pass

    def probe(self, point):
        self.probes.append(point)
        for label, (x, y) in self.keys.items():
            if abs(point[0] - x) <= 17.1 and abs(point[1] - (y + self.true_offset)) <= 22.15:
                return label
        return ""


ROWS = {"Q": [201.0, 90.8], "G": [201.0, 147.0], "Z": [201.0, 203.3]}
STACKED = {label: [x, y + 22.15] for label, (x, y) in ROWS.items()}


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.keys = keymap.single_char_keys(SNAPSHOT)

    def test_finds_the_true_offset(self):
        prober = FakeProber(self.keys, 488.4)
        found = keymap.calibrate(prober, self.keys, 874.0)
        offset, label = found["dy"], found["probe"]
        self.assertEqual(label, "G")
        self.assertLessEqual(abs(offset - 488.4), 22.15)

    def test_finds_a_zero_offset_without_probing_the_grid(self):
        keys = {"G": [201.0, 601.35]}
        prober = FakeProber(keys, 0.0)
        found = keymap.calibrate(prober, keys, 874.0)
        offset, _ = found["dy"], found["probe"]
        self.assertEqual(offset, 0.0)
        self.assertEqual(len(prober.probes), 1)

    def test_keeps_searching_past_a_neighbouring_key(self):
        """An offset that is wrong by one row types the row's neighbour. That is a miss, not a
        match, and the search must not stop on it."""
        prober = FakeProber(STACKED, 500.0)
        found = keymap.calibrate(prober, STACKED, 874.0, probe_label="G")
        offset, label = found["dy"], found["probe"]
        self.assertLessEqual(abs(offset - 500.0), 22.15)
        self.assertGreater(len(prober.probes), 1)

    def test_rejects_a_probe_key_that_is_not_on_screen(self):
        prober = FakeProber(self.keys, 0.0)
        with self.assertRaises(keymap.CalibrationFailed):
            keymap.calibrate(prober, self.keys, 874.0, probe_label="Z")

    def test_raises_when_nothing_types_itself(self):
        prober = FakeProber({}, 0.0)
        with self.assertRaises(keymap.CalibrationFailed):
            keymap.calibrate(prober, self.keys, 874.0)


class KeyFieldTests(unittest.TestCase):
    def test_the_field_is_the_bounding_box_of_the_key_centres(self):
        keys = keymap.single_char_keys(SNAPSHOT)
        x, y, width, height = keymap.key_field(keys)
        self.assertAlmostEqual(x, min(point[0] for point in keys.values()))
        self.assertAlmostEqual(y, min(point[1] for point in keys.values()))
        self.assertAlmostEqual(width, max(p[0] for p in keys.values()) - x)
        self.assertAlmostEqual(height, max(p[1] for p in keys.values()) - y)

    def test_one_key_cannot_define_a_field(self):
        with self.assertRaises(keymap.CalibrationFailed):
            keymap.key_field({"G": [10.0, 20.0]})

    def test_collinear_keys_cannot_define_a_field(self):
        with self.assertRaises(keymap.CalibrationFailed):
            keymap.key_field({"G": [10.0, 20.0], "H": [40.0, 20.0]})


class HorizontalOffsetTests(unittest.TestCase):
    """dx comes from the field's CENTRE, never its width.

    A bounding box of key centres is narrower than the keyboard by half a key each side, so a width
    test reads a correct full-width keyboard as narrow. Measured on an iPad Pro 11-inch
    (design.md V2): field w=595.0 on an 834.0 screen, and the true dx is 0.
    """

    def test_a_centred_field_needs_no_shift(self):
        self.assertEqual(keymap.derive_dx(201.0, 402.0), 0.0)

    def test_the_measured_ipad_case_yields_no_shift(self):
        self.assertEqual(keymap.derive_dx(419.0, 834.0), 0.0)

    def test_a_field_left_of_centre_shifts_right(self):
        self.assertAlmostEqual(keymap.derive_dx(300.0, 1024.0), 212.0)

    def test_a_field_right_of_centre_shifts_left(self):
        self.assertAlmostEqual(keymap.derive_dx(700.0, 1024.0), -188.0)

    def test_a_small_offset_snaps_to_zero(self):
        self.assertEqual(keymap.derive_dx(415.0, 834.0), 0.0)


class DxCalibrationTests(unittest.TestCase):
    def test_dx_is_zero_when_the_field_is_centred(self):
        keys = {"G": [201.0, 90.8], "H": [241.2, 147.0]}
        prober = FakeProber(keys, 560.0)
        found = keymap.calibrate(prober, keys, 874.0, screen_width=402.0, field_centre_x=201.0)
        self.assertEqual(found["dx"], 0.0)
        self.assertLessEqual(abs(found["dy"] - 560.0), keymap.GRID_STEP)

    def test_an_off_centre_field_is_reached_by_the_derived_dx(self):
        keys = {"G": [300.0, 90.8], "H": [340.0, 147.0]}
        shifted = {label: [x + 212.0, y] for label, (x, y) in keys.items()}
        prober = FakeProber(shifted, 900.0)
        found = keymap.calibrate(prober, keys, 1366.0, screen_width=1024.0, field_centre_x=300.0)
        self.assertAlmostEqual(found["dx"], 212.0)
        self.assertLessEqual(abs(found["dy"] - 900.0), keymap.GRID_STEP)

    def test_dx_falls_back_to_zero_when_the_derived_shift_does_not_type(self):
        keys = {"G": [300.0, 90.8], "H": [340.0, 147.0]}
        prober = FakeProber(keys, 900.0)
        found = keymap.calibrate(prober, keys, 1366.0, screen_width=1024.0, field_centre_x=300.0)
        self.assertEqual(found["dx"], 0.0)

    def test_no_screen_width_means_no_horizontal_search(self):
        keys = {"G": [201.0, 90.8], "H": [241.2, 147.0]}
        prober = FakeProber(keys, 560.0)
        self.assertEqual(keymap.calibrate(prober, keys, 874.0)["dx"], 0.0)

    def test_the_untouched_key_point_is_the_very_first_probe(self):
        """An app whose frames are already screen points must not pay the vertical ladder under a
        derived dx before offset zero is ever tried."""
        keys = {"G": [201.0, 601.35], "H": [241.2, 657.5]}
        prober = FakeProber(keys, 0.0)
        found = keymap.calibrate(prober, keys, 874.0, screen_width=1024.0, field_centre_x=100.0)
        self.assertEqual(prober.probes[0], [201.0, 601.35])
        self.assertEqual(len(prober.probes), 1)
        self.assertEqual((found["dx"], found["dy"]), (0.0, 0.0))

    def test_a_horizontal_shift_smaller_than_a_key_is_not_discarded(self):
        """dx=0 matches at the first offset where the probe still falls inside its own half-key hit
        area, so a real 15 pt shift must be tried BEFORE the vertical ladder is walked at dx=0."""
        keys = {"G": [186.0, 90.8], "H": [226.2, 147.0]}
        shifted = {label: [x + 15.0, y] for label, (x, y) in keys.items()}
        prober = FakeProber(shifted, 560.0)
        found = keymap.calibrate(prober, keys, 874.0, screen_width=402.0, field_centre_x=186.0)
        self.assertAlmostEqual(found["dx"], 15.0)
        self.assertLessEqual(abs(found["dy"] - 560.0), keymap.GRID_STEP)

    def test_a_degenerate_key_field_still_calibrates_vertically(self):
        """A single-row key set carries no dx information; that must skip dx, not abort the run."""
        keys = {"F": [161.0, 112.95], "G": [201.0, 112.95], "H": [241.0, 112.95]}
        prober = FakeProber(keys, 560.0)
        found = keymap.calibrate(prober, keys, 874.0, screen_width=402.0, field_centre_x=None)
        self.assertEqual(found["dx"], 0.0)
        self.assertLessEqual(abs(found["dy"] - 560.0), 22.15)


class FakeIdb:
    """A focused text field plus a keyboard: taps append whichever key sits under the point,
    backspace deletes, and `text` appends verbatim."""

    def __init__(self, keys, true_offset=0.0, value=""):
        self.keys = keys
        self.true_offset = true_offset
        self.value = value
        self.ops = []

    def send(self, ops, udid, timeout=None):
        results = []
        for step in ops:
            self.ops.append(step)
            kind = step.get("op")
            if kind == "value":
                results.append({"value": self.value})
                continue
            if kind == "key":
                self.value = self.value[:-1]
            elif kind == "text":
                self.value += step.get("text") or ""
            elif kind == "tapBurst":
                self.value += self.typed(step["points"][0])
            results.append({})
        return {"ok": True, "results": results}

    def typed(self, point):
        for label, (x, y) in self.keys.items():
            if abs(point[0] - x) <= 17.1 and abs(point[1] - (y + self.true_offset)) <= 22.15:
                return label
        return ""


def keyboard(*labels_at):
    rows = [element("staticText", label, [x - 17.1, y - 22.15, 34.2, 44.3])
            for label, x, y in labels_at]
    return rows + [{"type": "textField", "frame": [54.0, 418.3, 322.0, 44.0], "app": APP}]


TWO_ROW = keyboard(("G", 201.0, 112.95), ("H", 241.0, 169.15), ("F", 161.0, 169.15))
ONE_ROW = keyboard(("G", 201.0, 112.95), ("H", 241.0, 112.95), ("F", 161.0, 112.95))
SCREEN = {"w": 402.0, "h": 874.0, "fingerprint": "402x874"}


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.saved_send = keymap.idb.send
        self.addCleanup(setattr, keymap.idb, "send", self.saved_send)
        self.saved_save = keymap.records.save_key_map
        self.addCleanup(setattr, keymap.records, "save_key_map", self.saved_save)
        self.saved = []
        keymap.records.save_key_map = lambda app, keys, fingerprint, calibration=None: (
            self.saved.append((keys, fingerprint, calibration)) or "keymap.json"
        )

    def learn(self, snapshot, value=""):
        keys = keymap.single_char_keys(snapshot)
        fake = FakeIdb({label: [x, y] for label, (x, y) in keys.items()}, 561.05, value)
        keymap.idb.send = fake.send
        return fake, keymap.learn(APP, "UDID", snapshot=snapshot, screen=dict(SCREEN))

    def test_the_probes_own_characters_are_trimmed_off_the_field(self):
        fake, found = self.learn(TWO_ROW, value="hello")
        self.assertEqual(fake.value, "hello")
        self.assertEqual(found["probe"], "G")
        self.assertNotIn("text", [step.get("op") for step in fake.ops])

    def test_a_non_ascii_field_survives_because_it_is_never_retyped(self):
        """idb's HID `text` op drops non-ASCII, so re-typing would delete the user's Korean."""
        fake, _ = self.learn(TWO_ROW, value="안녕하세요")
        self.assertEqual(fake.value, "안녕하세요")
        self.assertNotIn("text", [step.get("op") for step in fake.ops])

    def test_a_failed_calibration_still_leaves_the_field_as_found(self):
        """The cleanup runs in a finally, so a raise midway cannot strand the probe's characters."""
        fake = FakeIdb({"H": [201.0, 169.15]}, 561.05, "café")
        keymap.idb.send = fake.send
        with self.assertRaises(keymap.CalibrationFailed):
            keymap.learn(APP, "UDID", snapshot=TWO_ROW, screen=dict(SCREEN))
        self.assertIn("key", [step.get("op") for step in fake.ops])
        self.assertEqual(fake.value, "café")

    def test_an_empty_field_is_left_empty(self):
        fake, _ = self.learn(TWO_ROW)
        self.assertEqual(fake.value, "")
        self.assertNotIn("text", [step.get("op") for step in fake.ops])

    def test_a_single_row_key_set_still_learns(self):
        fake, found = self.learn(ONE_ROW, value="hello")
        self.assertEqual(fake.value, "hello")
        self.assertEqual(found["dx"], 0.0)
        self.assertNotIn("field", self.saved[0][2])

    def test_a_two_row_key_set_records_its_field(self):
        self.learn(TWO_ROW)
        self.assertIn("field", self.saved[0][2])


class TrimTests(unittest.TestCase):
    def prober(self, value):
        fake = FakeIdb({}, 0.0, value)
        saved = keymap.idb.send
        self.addCleanup(setattr, keymap.idb, "send", saved)
        keymap.idb.send = fake.send
        return fake, keymap.Prober("UDID", [215.0, 440.3])

    def test_only_the_added_characters_are_backspaced(self):
        fake, prober = self.prober("안녕하세요GH")
        prober.trim_to("안녕하세요")
        self.assertEqual(fake.value, "안녕하세요")
        self.assertEqual(len([step for step in fake.ops if step.get("op") == "key"]), 2)
        self.assertNotIn("text", [step.get("op") for step in fake.ops])

    def test_an_unchanged_field_is_not_touched_at_all(self):
        fake, prober = self.prober("hello")
        prober.trim_to("hello")
        self.assertEqual(fake.value, "hello")
        self.assertNotIn("key", [step.get("op") for step in fake.ops])

    def test_a_prefix_autocorrect_away_falls_back_to_retyping(self):
        """Autocorrect can replace what was there, and then only the ASCII re-type is left."""
        fake, prober = self.prober("hellp G")
        prober.trim_to("hello")
        self.assertEqual(fake.value, "hello")
        self.assertIn("text", [step.get("op") for step in fake.ops])


class LegacyAdoptionTests(unittest.TestCase):
    LEGACY = {"A": [61.0, 674.0], "S": [101.0, 674.0], "D": [141.0, 674.0],
              "F": [181.0, 674.0], "G": [221.0, 674.0],
              "Z": [81.0, 730.2], "X": [121.0, 730.2], "C": [161.0, 730.2],
              "V": [201.0, 730.2], "B": [241.0, 730.2]}

    def setUp(self):
        saved = keymap.idb.send
        self.addCleanup(setattr, keymap.idb, "send", saved)

    def validates(self, live, value=""):
        fake = FakeIdb(live, 0.0, value)
        keymap.idb.send = fake.send
        return fake, keymap._validates("UDID", dict(self.LEGACY), TWO_ROW)

    def tapped(self, fake):
        return [step["points"][0][0] for step in fake.ops if step.get("op") == "tapBurst"]

    def test_probes_three_keys_spread_across_the_field(self):
        fake, ok = self.validates(dict(self.LEGACY))
        self.assertTrue(ok)
        xs = sorted(self.tapped(fake))
        self.assertEqual(len(xs), 3)
        self.assertGreater(xs[1] - xs[0], 20.0)
        self.assertGreater(xs[2] - xs[1], 20.0)

    def test_never_taps_an_edge_key(self):
        """An off-by-one-row miss on the outermost column hits the globe and leaves the keyboard."""
        fake, _ = self.validates(dict(self.LEGACY))
        self.assertNotIn(61.0, self.tapped(fake))
        self.assertNotIn(241.0, self.tapped(fake))

    def test_only_keys_with_a_neighbouring_row_are_probed(self):
        single_row = {label: [x, 674.0] for label, (x, _y) in LegacyAdoptionTests.LEGACY.items()}
        chosen = keymap._probe_labels(single_row)
        self.assertEqual(len(chosen), 3)
        self.assertNotIn(min(single_row, key=lambda label: single_row[label][0]), chosen)

    def test_a_map_right_at_the_probe_but_stale_elsewhere_is_rejected(self):
        probes = keymap._probe_labels(dict(self.LEGACY))
        live = dict(self.LEGACY)
        live[probes[-1]] = [400.0, live[probes[-1]][1]]
        _, ok = self.validates(live)
        self.assertFalse(ok)

    def test_the_first_mismatch_stops_the_probing(self):
        probes = keymap._probe_labels(dict(self.LEGACY))
        live = dict(self.LEGACY)
        live[probes[0]] = [400.0, live[probes[0]][1]]
        fake, ok = self.validates(live)
        self.assertFalse(ok)
        self.assertEqual(len(self.tapped(fake)), 1)

    def test_the_probes_own_characters_are_trimmed_off_the_field(self):
        fake, ok = self.validates(dict(self.LEGACY), value="안녕하세요")
        self.assertTrue(ok)
        self.assertEqual(fake.value, "안녕하세요")
        self.assertNotIn("text", [step.get("op") for step in fake.ops])


if __name__ == "__main__":
    unittest.main()
