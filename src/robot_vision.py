#!/usr/bin/env python3
"""Vision service: polls camera snapshots, detects faces, publishes telemetry."""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from config.vision import (
    DEFAULT_CONFIG_PATH,
    VisionConfig,
    VisionConfigError,
    load_vision_config,
)
from lib.log import setup_logging
from telemetry.messages import vision_update
from telemetry.paths import DEFAULT_PUBLISH_SOCKET
from telemetry.socket_client import publish_message


DEFAULT_CAMERA_URL = "http://127.0.0.1:8081/snapshot.jpg"
DEFAULT_SNAPSHOT_TIMEOUT = 1.0
CONFIG_POLL_INTERVAL = 1.0
DISABLED_PUBLISH_INTERVAL = 1.0

log = setup_logging("robot-vision")


PixelBox = tuple[int, int, int, int]
DetectorResult = tuple[list[PixelBox], int, int]
Detector = Callable[[bytes], DetectorResult]


class CameraFetchError(RuntimeError):
    """Raised when fetching a camera snapshot fails."""


class DetectorUnavailable(RuntimeError):
    """Raised when the face detector cannot be created or run."""


def normalize_box(box: PixelBox, image_width: int, image_height: int) -> dict[str, float]:
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")
    x, y, width, height = box
    return {
        "x": x / image_width,
        "y": y / image_height,
        "width": width / image_width,
        "height": height / image_height,
    }


def fetch_snapshot_http(url: str, timeout: float = DEFAULT_SNAPSHOT_TIMEOUT) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CameraFetchError(str(exc)) from exc


class HaarFaceDetector:
    """OpenCV Haar cascade face detector. Imports cv2 lazily so tests don't need it."""

    def __init__(self):
        try:
            import cv2  # type: ignore[import-not-found]
            import numpy  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DetectorUnavailable(f"OpenCV not installed: {exc}") from exc

        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            raise DetectorUnavailable(f"could not load Haar cascade at {cascade_path}")

        self._cv2 = cv2
        self._numpy = numpy
        self._cascade = cascade

    def __call__(self, jpeg: bytes) -> DetectorResult:
        cv2 = self._cv2
        numpy = self._numpy

        buffer = numpy.frombuffer(jpeg, dtype=numpy.uint8)
        image = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if image is None:
            raise CameraFetchError("could not decode jpeg")

        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        rects = self._cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5)
        faces = [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in rects]
        return faces, width, height


class VisionService:
    """One-loop face detection service. The run loop calls `tick()` and sleeps the returned seconds."""

    def __init__(
        self,
        *,
        config_path: str,
        camera_url: str,
        publish: Callable[[dict[str, Any]], Any],
        fetch_snapshot: Callable[[str], bytes],
        detector_factory: Callable[[], Detector],
        time_fn: Callable[[], float] = time.time,
    ):
        self.config_path = config_path
        self.camera_url = camera_url
        self.publish = publish
        self.fetch_snapshot = fetch_snapshot
        self.detector_factory = detector_factory
        self.time_fn = time_fn

        self.config = VisionConfig()
        self._config_mtime: float | None = None
        self._config_error: str | None = None
        self._detector: Detector | None = None
        self._detector_error: str | None = None
        self._last_detection_time: float | None = None
        self._last_disabled_publish: float | None = None
        self._next_detection_time: float | None = None

    def tick(self) -> float:
        """Run one iteration. Returns the recommended sleep before the next tick.

        Always returns at most CONFIG_POLL_INTERVAL so the run loop wakes about
        once per second to re-check the config file even when the detection
        period is longer than that (e.g. 0.2 Hz = 5s).
        """
        self._reload_config_if_changed()

        if self._config_error is not None:
            self._publish_status("error", error=self._config_error)
            return CONFIG_POLL_INTERVAL

        if not self.config.enabled:
            now = self.time_fn()
            if (
                self._last_disabled_publish is None
                or now - self._last_disabled_publish >= DISABLED_PUBLISH_INTERVAL
            ):
                self._publish_status("disabled")
                self._last_disabled_publish = now
            return CONFIG_POLL_INTERVAL

        now = self.time_fn()
        if self._next_detection_time is not None and now < self._next_detection_time:
            return min(self._next_detection_time - now, CONFIG_POLL_INTERVAL)

        if self._detector is None:
            try:
                self._detector = self.detector_factory()
                self._detector_error = None
            except DetectorUnavailable as exc:
                self._detector_error = str(exc)
                log.warning("face detector unavailable: %s", exc)
                self._publish_status("detector_unavailable", error=self._detector_error)
                self._next_detection_time = now + self._detection_period()
                return self._next_sleep(now)

        self._next_detection_time = now + self._detection_period()

        try:
            jpeg = self.fetch_snapshot(self.camera_url)
        except CameraFetchError as exc:
            self._publish_status("camera_unavailable", error=str(exc))
            return self._next_sleep(now)

        try:
            faces_pixels, image_width, image_height = self._detector(jpeg)
        except CameraFetchError as exc:
            self._publish_status("camera_unavailable", error=str(exc))
            return self._next_sleep(now)
        except DetectorUnavailable as exc:
            self._detector = None
            self._detector_error = str(exc)
            log.warning("face detector failed: %s", exc)
            self._publish_status("detector_unavailable", error=self._detector_error)
            return self._next_sleep(now)

        faces = [normalize_box(box, image_width, image_height) for box in faces_pixels]
        self._last_detection_time = now
        self.publish(
            vision_update(
                enabled=True,
                status="detecting",
                faces=faces,
                image_width=image_width,
                image_height=image_height,
                detection_rate_hz=self.config.detection_rate_hz,
                last_detection_time=self._last_detection_time,
            )
        )
        return self._next_sleep(now)

    def _detection_period(self) -> float:
        return 1.0 / self.config.detection_rate_hz

    def _next_sleep(self, now: float) -> float:
        if self._next_detection_time is None:
            return CONFIG_POLL_INTERVAL
        return min(max(self._next_detection_time - now, 0.0), CONFIG_POLL_INTERVAL)

    def _publish_status(self, status: str, error: str | None = None) -> None:
        self.publish(
            vision_update(
                enabled=self.config.enabled,
                status=status,
                faces=[],
                image_width=None,
                image_height=None,
                detection_rate_hz=self.config.detection_rate_hz,
                last_detection_time=self._last_detection_time,
                error=error,
            )
        )

    def _reload_config_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            mtime = None

        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime

        if mtime is None:
            self.config = VisionConfig()
            self._config_error = None
            self._next_detection_time = None
            return

        try:
            self.config = load_vision_config(self.config_path)
            self._config_error = None
            self._next_detection_time = None
            log.info(
                "vision config loaded: enabled=%s rate_hz=%.2f",
                self.config.enabled,
                self.config.detection_rate_hz,
            )
        except VisionConfigError as exc:
            self._config_error = str(exc)
            log.warning("vision config invalid, keeping last good config: %s", exc)


def run_loop(service: VisionService, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            sleep_seconds = service.tick()
        except Exception as exc:  # noqa: BLE001 -- never let the service die mid-loop
            log.exception("vision tick failed: %s", exc)
            sleep_seconds = CONFIG_POLL_INTERVAL
        stop.wait(sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Robot vision service.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--camera-url", default=DEFAULT_CAMERA_URL)
    parser.add_argument("--telemetry-socket", default=DEFAULT_PUBLISH_SOCKET)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    service = VisionService(
        config_path=args.config,
        camera_url=args.camera_url,
        publish=lambda message: publish_message(args.telemetry_socket, message),
        fetch_snapshot=fetch_snapshot_http,
        detector_factory=HaarFaceDetector,
    )

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log.info("vision service starting")
    run_loop(service, stop)
    log.info("vision service stopped")


if __name__ == "__main__":
    main()
