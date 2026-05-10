import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_vision import (
    CameraFetchError,
    DetectorUnavailable,
    HaarFaceDetector,
    VisionService,
    normalize_box,
)


def write_config(path: str, values: dict) -> None:
    with open(path, "w") as file_obj:
        json.dump(values, file_obj)


def bump_mtime(path: str, seconds: float = 2.0) -> None:
    """Bump mtime forward so the service notices a config change without flaky timing."""
    stat = os.stat(path)
    os.utime(path, (stat.st_atime, stat.st_mtime + seconds))


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_service(
    config_path: str,
    *,
    fetch_snapshot=None,
    detector_factory=None,
    clock=None,
):
    published: list[dict] = []
    fetched_urls: list[str] = []

    def default_fetch(url):
        fetched_urls.append(url)
        return b"fake-jpeg"

    def default_detector(_jpeg):
        return [(100, 200, 50, 80)], 1000, 800

    def default_factory():
        return default_detector

    service = VisionService(
        config_path=config_path,
        camera_url="http://camera/snapshot.jpg",
        publish=published.append,
        fetch_snapshot=fetch_snapshot or default_fetch,
        detector_factory=detector_factory or default_factory,
        time_fn=clock or FakeClock(),
    )
    return service, published, fetched_urls


class NormalizeBoxTest(unittest.TestCase):
    def test_normalize_box_scales_to_image_size(self):
        box = normalize_box((100, 200, 50, 80), 1000, 800)

        self.assertAlmostEqual(box["x"], 0.1)
        self.assertAlmostEqual(box["y"], 0.25)
        self.assertAlmostEqual(box["width"], 0.05)
        self.assertAlmostEqual(box["height"], 0.1)

    def test_normalize_box_rejects_zero_image_size(self):
        with self.assertRaises(ValueError):
            normalize_box((0, 0, 1, 1), 0, 100)


class HaarFaceDetectorTest(unittest.TestCase):
    def test_missing_cv2_data_reports_detector_unavailable(self):
        class FakeCascade:
            def __init__(self, _path):
                pass

            def empty(self):
                return True

        fake_cv2 = type(
            "FakeCv2",
            (),
            {"CascadeClassifier": FakeCascade},
        )()

        with mock.patch.dict(
            sys.modules, {"cv2": fake_cv2, "numpy": object()}
        ), self.assertRaises(DetectorUnavailable) as context:
            HaarFaceDetector()

        self.assertIn("could not load Haar cascade", str(context.exception))

    def test_uses_system_cascade_path_when_cv2_data_is_missing(self):
        used_paths: list[str] = []

        class FakeCascade:
            def __init__(self, path):
                used_paths.append(path)

            def empty(self):
                return False

        fake_cv2 = type(
            "FakeCv2",
            (),
            {"CascadeClassifier": FakeCascade},
        )()

        with mock.patch.dict(sys.modules, {"cv2": fake_cv2, "numpy": object()}), mock.patch(
            "robot_vision.os.path.exists",
            lambda path: path == "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        ):
            HaarFaceDetector()

        self.assertEqual(
            used_paths,
            ["/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"],
        )


class VisionServiceTest(unittest.TestCase):
    def test_disabled_mode_does_not_fetch_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": False, "detection_rate_hz": 2.0})

            service, published, fetched = make_service(config_path)
            sleep_seconds = service.tick()

        self.assertEqual(fetched, [])
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["status"], "disabled")
        self.assertFalse(published[0]["enabled"])
        self.assertEqual(published[0]["faces"], [])
        self.assertEqual(sleep_seconds, 1.0)

    def test_disabled_mode_publishes_at_most_once_per_second(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": False, "detection_rate_hz": 2.0})

            clock = FakeClock(start=100.0)
            service, published, _ = make_service(config_path, clock=clock)

            service.tick()
            clock.advance(0.2)
            service.tick()
            clock.advance(0.9)
            service.tick()

        self.assertEqual(len(published), 2)

    def test_enabled_mode_fetches_and_publishes_normalized_faces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 2.0})

            service, published, fetched = make_service(config_path)
            sleep_seconds = service.tick()

        self.assertEqual(fetched, ["http://camera/snapshot.jpg"])
        self.assertEqual(len(published), 1)
        message = published[0]
        self.assertEqual(message["status"], "detecting")
        self.assertEqual(message["image_width"], 1000)
        self.assertEqual(message["image_height"], 800)
        self.assertEqual(len(message["faces"]), 1)
        self.assertAlmostEqual(message["faces"][0]["x"], 0.1)
        self.assertAlmostEqual(message["faces"][0]["width"], 0.05)
        self.assertEqual(sleep_seconds, 0.5)

    def test_detected_faces_are_logged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 2.0})

            service, _published, _fetched = make_service(config_path)
            with self.assertLogs("robot-vision", level="INFO") as logs:
                service.tick()

        self.assertIn("detected 1 face(s)", "\n".join(logs.output))

    def test_camera_fetch_failure_publishes_camera_unavailable(self):
        def failing_fetch(_url):
            raise CameraFetchError("connection refused")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 2.0})

            clock = FakeClock(start=100.0)
            service, published, _ = make_service(
                config_path, fetch_snapshot=failing_fetch, clock=clock
            )
            service.tick()
            clock.advance(0.5)
            service.tick()

        self.assertEqual(len(published), 2)
        for message in published:
            self.assertEqual(message["status"], "camera_unavailable")
            self.assertEqual(message["error"], "connection refused")
            self.assertEqual(message["faces"], [])

    def test_detector_decode_failure_publishes_camera_unavailable(self):
        def decode_failing_detector(_jpeg):
            raise CameraFetchError("could not decode jpeg")

        def factory():
            return decode_failing_detector

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 2.0})

            service, published, fetched = make_service(config_path, detector_factory=factory)
            service.tick()

        self.assertEqual(fetched, ["http://camera/snapshot.jpg"])
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["status"], "camera_unavailable")
        self.assertEqual(published[0]["error"], "could not decode jpeg")

    def test_detector_factory_unavailable_publishes_detector_unavailable(self):
        def failing_factory():
            raise DetectorUnavailable("OpenCV not installed: no module")

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 2.0})

            service, published, fetched = make_service(
                config_path, detector_factory=failing_factory
            )
            service.tick()

        self.assertEqual(fetched, [])
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["status"], "detector_unavailable")
        self.assertIn("OpenCV", published[0]["error"])

    def test_detector_runtime_failure_publishes_detector_unavailable(self):
        def detector(_jpeg):
            raise DetectorUnavailable("cascade missing")

        def factory():
            return detector

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 2.0})

            service, published, _ = make_service(config_path, detector_factory=factory)
            service.tick()

        self.assertEqual(published[-1]["status"], "detector_unavailable")
        self.assertEqual(published[-1]["error"], "cascade missing")

    def test_invalid_config_keeps_last_good_config_and_publishes_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 4.0})

            service, published, _ = make_service(config_path)
            service.tick()
            self.assertEqual(published[-1]["status"], "detecting")
            self.assertEqual(service.config.detection_rate_hz, 4.0)

            with open(config_path, "w") as file_obj:
                file_obj.write("{not valid json")
            bump_mtime(config_path)

            service.tick()

        self.assertEqual(published[-1]["status"], "error")
        self.assertEqual(service.config.detection_rate_hz, 4.0)
        self.assertTrue(service.config.enabled)

    def test_low_detection_rate_wakes_about_once_per_second(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 0.2})

            clock = FakeClock(start=100.0)
            service, _published, fetched = make_service(config_path, clock=clock)

            sleeps = []
            for _ in range(6):
                sleeps.append(service.tick())
                clock.advance(1.0)

        for sleep_seconds in sleeps:
            self.assertLessEqual(sleep_seconds, 1.0)
        self.assertEqual(len(fetched), 2)

    def test_missing_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "missing.json")

            service, published, fetched = make_service(config_path)
            service.tick()

        self.assertEqual(len(fetched), 1)
        self.assertEqual(published[-1]["status"], "detecting")
        self.assertEqual(published[-1]["detection_rate_hz"], 2.0)


if __name__ == "__main__":
    unittest.main()
