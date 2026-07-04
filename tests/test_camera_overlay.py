import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from voice.camera_overlay import angle_to_x, annotate_snapshot

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


if __name__ == "__main__":
    unittest.main()
