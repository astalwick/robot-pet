"""
Pi camera driver.

Pure-Python wrapper around picamera2. Single consumer: holds an exclusive lock
on the camera device while started. JPEG encoding happens inside picamera2's
encoder thread (turbojpeg), so the caller pays no per-frame Python cost.

picamera2 is installed on Raspberry Pi OS via apt:

    sudo apt install -y python3-picamera2

For the venv to see it, recreate the venv with --system-site-packages, or
pip install picamera2 directly into the venv.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any


log = logging.getLogger(__name__)


class CameraUnavailable(RuntimeError):
    """Raised when picamera2/libcamera is missing or no camera is detected."""


def _make_sink_output(sink: Callable[[bytes], None]):
    """Build a picamera2 Output that forwards each encoded JPEG to `sink`."""
    from picamera2.outputs import Output

    class SinkOutput(Output):
        def outputframe(self, frame, keyframe=True, timestamp=None, **kwargs):
            if self.recording:
                sink(bytes(frame))

    return SinkOutput()


class CameraDriver:
    """Streams JPEG frames from a Pi camera via picamera2's encoder pipeline."""

    def __init__(
        self,
        size: tuple[int, int] = (1280, 720),
        jpeg_quality: int = 75,
        sensor_size: tuple[int, int] | None = (2304, 1296),
        fps: float = 10.0,
    ):
        self.size = size
        self.jpeg_quality = jpeg_quality
        self.sensor_size = sensor_size
        self.fps = fps
        self._picam: Any | None = None

    def start(self, sink: Callable[[bytes], None]) -> None:
        """Open the camera and stream encoded JPEGs to `sink`.

        `sink` is invoked from picamera2's encoder thread; it must be thread-safe.
        """
        try:
            from picamera2 import Picamera2
            from picamera2.encoders import JpegEncoder
        except ImportError as exc:
            raise CameraUnavailable(
                "picamera2 not installed (sudo apt install python3-picamera2)"
            ) from exc

        frame_us = int(round(1_000_000 / self.fps)) if self.fps > 0 else 0
        controls = {"FrameDurationLimits": (frame_us, frame_us)} if frame_us > 0 else {}

        picam: Any | None = None
        try:
            picam = Picamera2()
            kwargs: dict[str, Any] = {
                "main": {"size": self.size, "format": "RGB888"},
                "controls": controls,
            }
            if self.sensor_size is not None:
                kwargs["raw"] = {"size": self.sensor_size}
            config = picam.create_video_configuration(**kwargs)
            picam.configure(config)
            log.info("camera configured: %s", picam.camera_configuration())
            picam.start_recording(JpegEncoder(q=self.jpeg_quality), _make_sink_output(sink))
        except Exception as exc:  # noqa: BLE001 -- libcamera throws varied types
            if picam is not None:
                try:
                    picam.close()
                except Exception:  # noqa: BLE001
                    log.exception("camera cleanup after failed start failed")
            raise CameraUnavailable(f"failed to open camera: {exc}") from exc

        self._picam = picam

    def stop(self) -> None:
        """Release the camera."""
        if self._picam is None:
            return
        try:
            self._picam.stop_recording()
            self._picam.close()
        except Exception:  # noqa: BLE001
            log.exception("camera stop failed")
        self._picam = None
