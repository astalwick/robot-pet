import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.vision import VisionConfig
from robot_vision import (
    CameraFetchError,
    DetectorUnavailable,
    GrayFrame,
    HaarFaceDetector,
    VisionService,
    fetch_snapshot_http,
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
    profile_every=0,
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
        camera_url="http://camera/vision.gray",
        publish=published.append,
        fetch_snapshot=fetch_snapshot or default_fetch,
        detector_factory=detector_factory or default_factory,
        time_fn=clock or FakeClock(),
        profile_every=profile_every,
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


class FetchSnapshotHttpTest(unittest.TestCase):
    def test_gray8_response_returns_frame_with_dimensions(self):
        class FakeResponse:
            headers = {
                "X-Frame-Format": "gray8",
                "X-Frame-Width": "2",
                "X-Frame-Height": "2",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"\x00\x01\x02\x03"

        with mock.patch("robot_vision.urllib.request.urlopen", return_value=FakeResponse()):
            frame = fetch_snapshot_http("http://camera/vision.gray")

        self.assertEqual(frame, GrayFrame(b"\x00\x01\x02\x03", 2, 2))


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

    def test_large_images_are_downscaled_for_detection(self):
        detect_shapes = []

        class FakeImage:
            def __init__(self, height, width, channels=None):
                self.shape = (height, width) if channels is None else (height, width, channels)

        class FakeCascade:
            def __init__(self, _path):
                pass

            def empty(self):
                return False

            def detectMultiScale(self, image, **_kwargs):
                detect_shapes.append(image.shape)
                return [(50, 60, 40, 30)]

        class FakeCv2:
            IMREAD_GRAYSCALE = 0
            INTER_AREA = 3

            def CascadeClassifier(self, path):
                return FakeCascade(path)

            def imdecode(self, _buffer, _mode):
                return FakeImage(720, 1280)

            def resize(self, _image, size, interpolation=None):
                return FakeImage(size[1], size[0])

        class FakeNumpy:
            uint8 = object()

            def frombuffer(self, data, dtype=None):
                return data

        with mock.patch.dict(
            sys.modules, {"cv2": FakeCv2(), "numpy": FakeNumpy()}
        ), mock.patch("robot_vision.os.path.exists", lambda _path: True):
            detector = HaarFaceDetector()
            faces, image_width, image_height = detector(b"jpeg")

        self.assertEqual(detect_shapes, [(360, 640)])
        self.assertEqual((image_width, image_height), (1280, 720))
        self.assertEqual(faces, [(100, 120, 80, 60)])

    def test_gray_frames_skip_jpeg_decode(self):
        detect_shapes = []

        class FakeCascade:
            def __init__(self, _path):
                pass

            def empty(self):
                return False

            def detectMultiScale(self, image, **_kwargs):
                detect_shapes.append(image.shape)
                return [(10, 20, 30, 40)]

        class FakeCv2:
            INTER_AREA = 3

            def CascadeClassifier(self, path):
                return FakeCascade(path)

        class FakeBuffer:
            def reshape(self, shape):
                self.shape = shape
                return self

        class FakeNumpy:
            uint8 = object()

            def frombuffer(self, _data, dtype=None):
                return FakeBuffer()

        with mock.patch.dict(
            sys.modules, {"cv2": FakeCv2(), "numpy": FakeNumpy()}
        ), mock.patch("robot_vision.os.path.exists", lambda _path: True):
            detector = HaarFaceDetector()
            faces, image_width, image_height = detector(GrayFrame(b"\x00" * (320 * 180), 320, 180))

        self.assertEqual(detect_shapes, [(180, 320)])
        self.assertEqual((image_width, image_height), (320, 180))
        self.assertEqual(faces, [(10, 20, 30, 40)])


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

        self.assertEqual(fetched, ["http://camera/vision.gray"])
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

    def test_profile_logs_stage_timings_when_enabled(self):
        class ProfilingDetector:
            last_profile = {"prep": 0.001, "resize": 0.002, "detect": 0.003}

            def __call__(self, _frame):
                return [(100, 200, 50, 80)], 1000, 800

        def factory():
            return ProfilingDetector()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(config_path, {"enabled": True, "detection_rate_hz": 2.0})

            service, _published, _fetched = make_service(
                config_path,
                detector_factory=factory,
                profile_every=1,
            )
            with self.assertLogs("robot-vision", level="INFO") as logs:
                service.tick()

        output = "\n".join(logs.output)
        self.assertIn("vision profile:", output)
        self.assertIn("fetch=", output)
        self.assertIn("prep=", output)
        self.assertIn("resize=", output)
        self.assertIn("detect=", output)

    def test_detector_tuning_config_is_applied_live(self):
        seen = {}

        class ConfigurableDetector:
            detection_max_width = 640
            haar_scale_factor = 1.1
            haar_min_size = 24

            def __call__(self, _frame):
                seen["detection_max_width"] = self.detection_max_width
                seen["haar_scale_factor"] = self.haar_scale_factor
                seen["haar_min_size"] = self.haar_min_size
                return [], 1000, 800

        def factory():
            return ConfigurableDetector()

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "vision.json")
            write_config(
                config_path,
                {
                    "enabled": True,
                    "detection_rate_hz": 2.0,
                    "detection_max_width": 480,
                    "haar_scale_factor": 1.2,
                    "haar_min_size": 32,
                },
            )

            service, _published, _fetched = make_service(config_path, detector_factory=factory)
            service.tick()

        self.assertEqual(seen["detection_max_width"], 480)
        self.assertEqual(seen["haar_scale_factor"], 1.2)
        self.assertEqual(seen["haar_min_size"], 32)

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

        self.assertEqual(fetched, ["http://camera/vision.gray"])
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
        self.assertEqual(published[-1]["detection_rate_hz"], VisionConfig().detection_rate_hz)


if __name__ == "__main__":
    unittest.main()
