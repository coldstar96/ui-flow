import importlib.util
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_sidecar():
    """fb-idb lives in a managed venv this suite never has, so its imports are stubbed."""
    types_module = types.ModuleType("idb.common.types")
    for name in ("AccessibilityInfoOptions", "AccessibilityPoint", "HIDDelay", "HIDDirection",
                 "HIDPress", "HIDTouch", "Point", "TCPAddress"):
        setattr(types_module, name, type(name, (), {"__init__": lambda self, **kw: None}))
    common = types.ModuleType("idb.common")
    common.types = types_module
    client = types.ModuleType("idb.grpc.client")
    client.Client = type("Client", (), {})
    grpc = types.ModuleType("idb.grpc")
    grpc.client = client
    package = types.ModuleType("idb")
    package.common, package.grpc = common, grpc
    for name, module in (("idb", package), ("idb.common", common), ("idb.common.types", types_module),
                         ("idb.grpc", grpc), ("idb.grpc.client", client)):
        sys.modules[name] = module
    spec = importlib.util.spec_from_file_location(
        "uiflow_idb_sidecar_under_test", os.path.join(ROOT, "lib", "uiflow_idb_sidecar.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sidecar = load_sidecar()
SCREEN = {"w": 402.0, "h": 874.0}


class NeedsScreenTests(unittest.TestCase):
    def test_an_absolute_step_does_not_need_the_screen(self):
        self.assertFalse(sidecar.needs_screen({"op": "tap", "x": 1, "y": 2}))

    def test_a_normalized_step_needs_the_screen(self):
        self.assertTrue(sidecar.needs_screen({"op": "tap", "nx": 0.5, "ny": 0.5}))

    def test_a_burst_with_normalized_points_needs_the_screen(self):
        self.assertTrue(sidecar.needs_screen({"op": "tapBurst", "points": [{"nx": 0.1, "ny": 0.2}]}))

    def test_a_burst_of_pairs_does_not(self):
        self.assertFalse(sidecar.needs_screen({"op": "tapBurst", "points": [[1, 2]]}))

    def test_a_keymap_burst_does_not(self):
        self.assertFalse(sidecar.needs_screen({"op": "tapBurst", "keys": ["G"], "keyMap": {"G": [1, 2]}}))


class PointResolutionTests(unittest.TestCase):
    def test_absolute_coordinates_are_unchanged(self):
        self.assertEqual(sidecar.resolve_point({"op": "tap", "x": 155, "y": 437}, None), (155.0, 437.0))

    def test_normalized_coordinates_scale_to_the_live_screen(self):
        self.assertEqual(sidecar.resolve_point({"op": "tap", "nx": 0.5, "ny": 0.5}, SCREEN), (201.0, 437.0))

    def test_the_two_axes_are_independent(self):
        self.assertEqual(sidecar.resolve_point({"op": "tap", "x": 155, "ny": 0.25}, SCREEN), (155.0, 218.5))

    def test_supplying_both_forms_for_one_axis_is_refused(self):
        with self.assertRaises(sidecar.StepFailed) as caught:
            sidecar.resolve_point({"op": "tap", "x": 155, "nx": 0.5, "y": 1}, SCREEN)
        self.assertEqual(caught.exception.reason, "badStep")

    def test_a_missing_axis_is_refused(self):
        with self.assertRaises(sidecar.StepFailed):
            sidecar.resolve_point({"op": "tap", "x": 155}, SCREEN)

    def test_normalized_without_screen_metrics_is_refused(self):
        with self.assertRaises(sidecar.StepFailed) as caught:
            sidecar.resolve_point({"op": "tap", "nx": 0.5, "ny": 0.5}, None)
        self.assertEqual(caught.exception.reason, "noScreen")

    def test_a_fraction_outside_the_unit_range_is_refused(self):
        with self.assertRaises(sidecar.StepFailed):
            sidecar.resolve_point({"op": "tap", "nx": 1.4, "ny": 0.5}, SCREEN)

    def test_swipe_resolves_each_end_on_its_own_keys(self):
        step = {"op": "swipe", "fromX": 10, "fromY": 20, "toNx": 0.5, "toNy": 1.0}
        self.assertEqual(sidecar.resolve_point(step, SCREEN, "fromX", "fromY"), (10.0, 20.0))
        self.assertEqual(sidecar.resolve_point(step, SCREEN, "toX", "toY"), (201.0, 874.0))


class BurstPointTests(unittest.TestCase):
    def test_pairs_stay_absolute(self):
        self.assertEqual(sidecar.burst_points({"points": [[10, 20], [30, 40]]}, None),
                         [(10.0, 20.0), (30.0, 40.0)])

    def test_dict_entries_may_be_normalized(self):
        self.assertEqual(sidecar.burst_points({"points": [{"nx": 0.5, "ny": 0.5}]}, SCREEN),
                         [(201.0, 437.0)])

    def test_a_key_map_still_resolves_by_label(self):
        self.assertEqual(sidecar.burst_points({"keys": ["G"], "keyMap": {"G": [166.0, 742.3]}}, None),
                         [(166.0, 742.3)])

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(sidecar.StepFailed) as caught:
            sidecar.burst_points({"keys": ["Z"], "keyMap": {"G": [1.0, 2.0]}}, None)
        self.assertEqual(caught.exception.reason, "notFound")

    def test_a_burst_with_neither_points_nor_keys_is_refused(self):
        with self.assertRaises(sidecar.StepFailed):
            sidecar.burst_points({}, None)


if __name__ == "__main__":
    unittest.main()
