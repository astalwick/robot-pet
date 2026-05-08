"""
Pi camera driver.

Pure-Python wrapper around picamera2. Single consumer: holds an exclusive lock
on the camera device while started. To support multiple consumers later, wrap
one instance of this driver in a service that fans frames out over IPC; the
driver itself does not change.

picamera2 is installed on Raspberry Pi OS via apt:

    sudo apt install -y python3-picamera2

For the venv to see it, recreate the venv with --system-site-packages, or
pip install picamera2 directly into the venv.
"""

from __future__ import annotations

import io
import logging
from typing import Any


log = logging.getLogger(__name__)


class CameraUnavailable(RuntimeError):
    """Raised when picamera2/libcamera is missing or no camera is detected."""


class CameraDriver:
    """RGB frame capture from a Pi camera via picamera2."""

    def __init__(
        self,
        size: tuple[int, int] = (1280, 720),
        jpeg_quality: int = 75,
        sensor_size: tuple[int, int] | None = (2304, 1296),
    ):
        self.size = size
        self.jpeg_quality = jpeg_quality
        self.sensor_size = sensor_size
        self._picam: Any | None = None

    def start(self) -> None:
        """Open and configure the camera. Raises CameraUnavailable on failure."""
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraUnavailable(
                "picamera2 not installed (sudo apt install python3-picamera2)"
            ) from exc

        picam: Any | None = None
        try:
            picam = Picamera2()
            kwargs: dict[str, Any] = {"main": {"size": self.size, "format": "RGB888"}}
            if self.sensor_size is not None:
                kwargs["raw"] = {"size": self.sensor_size}
            config = picam.create_video_configuration(**kwargs)
            picam.configure(config)
            picam.options["quality"] = self.jpeg_quality
            log.info("camera configured: %s", picam.camera_configuration())
            picam.start()
        except Exception as exc:  # noqa: BLE001 -- libcamera throws varied types
            if picam is not None:
                try:
                    picam.close()
                except Exception:  # noqa: BLE001
                    log.exception("camera cleanup after failed start failed")
            raise CameraUnavailable(f"failed to open camera: {exc}") from exc

        self._picam = picam

    def capture_array(self):
        """Capture a single frame as an (H, W, 3) uint8 RGB numpy array."""
        if self._picam is None:
            raise RuntimeError("CameraDriver.start() must be called before capture_array()")
        # picamera2 quirk: format="RGB888" returns arrays in BGR memory order.
        return self._picam.capture_array()[..., ::-1]

    def capture_jpeg(self) -> bytes:
        """Capture a single frame encoded as JPEG bytes."""
        if self._picam is None:
            raise RuntimeError("CameraDriver.start() must be called before capture_jpeg()")
        buffer = io.BytesIO()
        self._picam.capture_file(buffer, format="jpeg")
        return buffer.getvalue()

    def stop(self) -> None:
        """Release the camera."""
        if self._picam is None:
            return
        try:
            self._picam.stop()
            self._picam.close()
        except Exception:  # noqa: BLE001
            log.exception("camera stop failed")
        self._picam = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
