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

import logging
from typing import Any


log = logging.getLogger(__name__)


class CameraUnavailable(RuntimeError):
    """Raised when picamera2/libcamera is missing or no camera is detected."""


class CameraDriver:
    """RGB frame capture from a Pi camera via picamera2."""

    def __init__(self, size: tuple[int, int] = (320, 240)):
        self.size = size
        self._picam: Any | None = None

    def start(self) -> None:
        """Open and configure the camera. Raises CameraUnavailable on failure."""
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraUnavailable(
                "picamera2 not installed (sudo apt install python3-picamera2)"
            ) from exc

        try:
            picam = Picamera2()
            config = picam.create_video_configuration(
                main={"size": self.size, "format": "RGB888"}
            )
            picam.configure(config)
            picam.start()
        except Exception as exc:  # noqa: BLE001 -- libcamera throws varied types
            raise CameraUnavailable(f"failed to open camera: {exc}") from exc

        self._picam = picam

    def capture_array(self):
        """Capture a single frame as an (H, W, 3) uint8 RGB numpy array."""
        if self._picam is None:
            raise RuntimeError("CameraDriver.start() must be called before capture_array()")
        # picamera2 quirk: format="RGB888" returns arrays in BGR memory order.
        return self._picam.capture_array()[..., ::-1]

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
