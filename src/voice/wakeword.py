"""Local wake-word detection via openWakeWord."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from drivers.respeaker import MIC_BLOCKSIZE

FRAME_SAMPLES = MIC_BLOCKSIZE


def pcm16_rms(frame: bytes) -> int:
    samples = np.frombuffer(frame, dtype=np.int16)
    if len(samples) == 0:
        return 0
    return int(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def ensure_feature_models() -> None:
    import openwakeword
    from openwakeword.utils import download_models

    resources = Path(openwakeword.__file__).parent / "resources" / "models"
    if (resources / "melspectrogram.onnx").exists() and (resources / "embedding_model.onnx").exists():
        return
    download_models(model_names=["_skip_pretrained_"])


class WakeWordDetector:
    def __init__(
        self,
        model_path: str,
        threshold: float = 0.5,
        debounce_secs: float = 2.0,
        rms_gate_min: int = 0,
    ) -> None:
        self.model_path = model_path
        self.threshold = threshold
        self.debounce_secs = debounce_secs
        self.rms_gate_min = rms_gate_min
        self._model = None
        self._wake_name: str | None = None
        self.last_score = 0.0
        self.last_rms = 0
        self.fire_count = 0
        self.last_fire_at = 0.0
        self.last_predict_seconds = 0.0

    def load(self) -> str:
        from openwakeword.model import Model

        model_path = Path(self.model_path)
        if not model_path.is_file():
            raise FileNotFoundError(f"Wake model not found: {model_path}")
        ensure_feature_models()
        self._model = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
        names = list(self._model.models.keys())
        if len(names) != 1:
            raise RuntimeError(f"Expected one wake model, got {names}")
        self._wake_name = names[0]
        return self._wake_name

    def reset(self) -> None:
        # Called when re-arming after a session. The model is not fed during a
        # session, so its streaming buffer holds stale pre-session audio that
        # would score spuriously on the first frames back. Clear it, and start a
        # debounce window so the session-end chime tail can't fire a wake either.
        if self._model is not None:
            self._model.reset()
        self.last_fire_at = time.monotonic()

    def score(self, frame: bytes) -> float:
        if self._model is None or self._wake_name is None:
            raise RuntimeError("WakeWordDetector.load() must be called first")
        samples = np.frombuffer(frame, dtype=np.int16)
        if len(samples) != FRAME_SAMPLES:
            return 0.0
        started = time.perf_counter()
        score = float(self._model.predict(samples)[self._wake_name])
        self.last_predict_seconds = time.perf_counter() - started
        self.last_score = score
        return score

    def check(self, frame: bytes, *, now: float | None = None) -> bool:
        self.last_rms = pcm16_rms(frame)
        if self.rms_gate_min > 0 and self.last_rms < self.rms_gate_min:
            self.last_score = 0.0
            self.last_predict_seconds = 0.0
            return False
        score = self.score(frame)
        if score < self.threshold:
            return False
        when = time.monotonic() if now is None else now
        if when - self.last_fire_at < self.debounce_secs:
            return False
        self.last_fire_at = when
        self.fire_count += 1
        return True
