import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.camera_overlay import (
    SENSED_CORRIDOR_MIN_M,
    _corridor_half_angle,
    angle_to_x,
    annotate_snapshot,
    nearest_forward_clearance_m,
)
from voice.assistant import forward_clearances

try:
    import cv2
    import numpy as np

    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


class AngleToXTest(unittest.TestCase):
    def test_center_maps_to_middle(self):
        self.assertEqual(angle_to_x(0.0, 640), 320)

    def test_left_edge_maps_to_zero(self):
        self.assertEqual(angle_to_x(51.0, 640), 0)

    def test_right_edge_maps_to_width(self):
        self.assertEqual(angle_to_x(-51.0, 640), 640)

    def test_positive_angle_is_left_of_center(self):
        left = angle_to_x(20.0, 640)
        center = angle_to_x(0.0, 640)
        right = angle_to_x(-20.0, 640)
        self.assertLess(left, center)
        self.assertGreater(right, center)


class ForwardClearancesTest(unittest.TestCase):
    def test_maps_forward_readings_to_left_center_right(self):
        clearances = forward_clearances(
            {
                "ok": True,
                "sensors": {
                    "readings": [
                        {"name": "front_left", "role": "forward", "clearance_m": 0.42, "ok": True},
                        {"name": "front_center", "role": "forward", "clearance_m": 0.90, "ok": True},
                        {"name": "front_right", "role": "forward", "ok": False},
                    ]
                },
            }
        )
        self.assertEqual(clearances["left"], 0.42)
        self.assertEqual(clearances["center"], 0.90)
        self.assertIsNone(clearances["right"])

    def test_no_forward_readings_gives_all_none(self):
        self.assertEqual(
            forward_clearances({"ok": True, "sensors": {"readings": []}}),
            {"left": None, "center": None, "right": None},
        )


class NearestClearanceTest(unittest.TestCase):
    def test_uses_minimum_available_clearance(self):
        self.assertEqual(
            nearest_forward_clearance_m({"left": 0.42, "center": 0.90, "right": 0.31}),
            0.31,
        )

    def test_sensed_pair_uses_minimum_half_angle(self):
        distance = 0.31
        self.assertAlmostEqual(_corridor_half_angle(distance), 28.17, places=1)
        self.assertLess(angle_to_x(_corridor_half_angle(distance), 640), angle_to_x(0.0, 640))

    def test_sub_minimum_clearance_skips_sensed_pair_distance(self):
        self.assertLess(0.10, SENSED_CORRIDOR_MIN_M)


@unittest.skipUnless(HAS_CV2, "opencv not installed")
class AnnotateSnapshotTest(unittest.TestCase):
    def _synthetic_jpeg(self, width: int = 320, height: int = 240) -> bytes:
        image = np.zeros((height, width, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", image)
        self.assertTrue(ok)
        return encoded.tobytes()

    def test_returns_valid_jpeg_with_same_dimensions(self):
        original = self._synthetic_jpeg(320, 240)
        annotated = annotate_snapshot(original)
        self.assertIsInstance(annotated, bytes)
        self.assertNotEqual(annotated, b"")

        original_image = cv2.imdecode(np.frombuffer(original, dtype=np.uint8), cv2.IMREAD_COLOR)
        annotated_image = cv2.imdecode(np.frombuffer(annotated, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(original_image)
        self.assertIsNotNone(annotated_image)
        self.assertEqual(original_image.shape, annotated_image.shape)

    def test_garbage_bytes_returned_unchanged(self):
        garbage = b"not a jpeg"
        self.assertIs(annotate_snapshot(garbage), garbage)

    def test_clearances_still_return_valid_jpeg(self):
        original = self._synthetic_jpeg()
        annotated = annotate_snapshot(
            original,
            {"left": 0.42, "center": 0.90, "right": 0.31},
        )
        image = cv2.imdecode(np.frombuffer(annotated, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(image)

    def test_none_clearances_matches_previous_behavior(self):
        original = self._synthetic_jpeg()
        self.assertEqual(annotate_snapshot(original), annotate_snapshot(original, None))


if __name__ == "__main__":
    unittest.main()
