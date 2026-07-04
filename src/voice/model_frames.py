"""Disk ring buffer of camera frames sent to the voice model."""

from __future__ import annotations

import time
from pathlib import Path

from lib.log import setup_logging

log = setup_logging("robot-voice")

MODEL_FRAMES_DIR = Path("/tmp/robot-pet-model-frames")
MAX_FRAMES = 40


def save_model_frame(jpeg_bytes: bytes, label: str, caption: str = "") -> None:
    """Save a frame that was sent to the model. Never raises."""
    try:
        base_dir = MODEL_FRAMES_DIR
        base_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{int(time.time() * 1000)}-{label}"
        (base_dir / f"{stem}.jpg").write_bytes(jpeg_bytes)
        if caption:
            (base_dir / f"{stem}.txt").write_text(caption, encoding="utf-8")
        jpgs = sorted(base_dir.glob("*.jpg"), key=lambda path: path.name)
        for old in jpgs[:-MAX_FRAMES]:
            old.unlink(missing_ok=True)
            sidecar = old.with_suffix(".txt")
            if sidecar.exists():
                sidecar.unlink()
    except OSError as exc:
        log.warning("model frame save failed: %s", exc)
