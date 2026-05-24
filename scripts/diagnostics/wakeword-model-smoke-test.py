#!/usr/bin/env python3
"""
Smoke-test a custom openWakeWord ONNX model (e.g. models/wake/Hey_Bloop.onnx).

Not yet in pyproject.toml — install before running:
    python -m pip install openwakeword

openwakeword pulls in onnxruntime on macOS/Windows. On Linux (Pi) it also installs
tflite-runtime; ONNX-only custom models work without tflite.

Versions verified on Mac (arm64, Python 3.12):
    openwakeword==0.6.0
    onnxruntime==1.26.0

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/wakeword-model-smoke-test.py
    python scripts/diagnostics/wakeword-model-smoke-test.py --list-devices
    python scripts/diagnostics/wakeword-model-smoke-test.py --mic
    python scripts/diagnostics/wakeword-model-smoke-test.py --mic --device XVF3800

On macOS, the app running Python (Terminal, iTerm, Cursor, etc.) needs microphone
access in System Settings → Privacy & Security → Microphone. If peak stays near 0
while you speak, fix permissions or pick a device with --list-devices / --device.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = REPO_ROOT / "models" / "wake" / "Hey_Bloop.onnx"
FRAME_SAMPLES = 1280  # 80 ms @ 16 kHz — matches ReSpeaker MIC_BLOCKSIZE


def ensure_feature_models() -> None:
    import openwakeword
    from openwakeword.utils import download_models

    resources = Path(openwakeword.__file__).parent / "resources" / "models"
    if (resources / "melspectrogram.onnx").exists() and (resources / "embedding_model.onnx").exists():
        return
    print("Downloading openWakeWord melspectrogram + embedding models (one-time)...")
    # Non-empty model_names skips bundled hey_jarvis etc.; feature models always download.
    download_models(model_names=["_smoke_test_skip_pretrained_"])


def load_model(model_path: Path):
    from openwakeword.model import Model

    if not model_path.is_file():
        raise FileNotFoundError(f"Wake model not found: {model_path}")
    ensure_feature_models()
    model = Model(wakeword_models=[str(model_path)])
    names = list(model.models.keys())
    if len(names) != 1:
        raise RuntimeError(f"Expected one wake model, got {names}")
    return model, names[0]


def list_input_devices() -> None:
    import sounddevice as sd

    default_in, _ = sd.default.device
    print("Input devices (→ = system default input):")
    for index, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] > 0:
            mark = "→" if index == default_in else " "
            print(
                f"  {mark} [{index}] {info['name']} "
                f"({info['max_input_channels']} in @ {info['default_samplerate']:.0f} Hz)"
            )


def resolve_input_device(device_substring: str | None) -> tuple[int | None, str]:
    import sounddevice as sd

    if device_substring:
        matches = [
            index
            for index, info in enumerate(sd.query_devices())
            if device_substring.lower() in info["name"].lower() and info["max_input_channels"] > 0
        ]
        if not matches:
            raise RuntimeError(f"No input device matching {device_substring!r}; use --list-devices")
        index = matches[0]
    else:
        index = sd.default.device[0]
        if index is None or index < 0:
            raise RuntimeError("No default input device; use --list-devices and --device")
    name = sd.query_devices(index)["name"]
    return index, name


def capture_channels_for_device(input_device: int | None) -> tuple[int, int]:
    """MacBook mic: mono. ReSpeaker: 6-channel interleaved, use processed channel 1."""
    import sounddevice as sd

    info = sd.query_devices(input_device, "input")
    if info["max_input_channels"] >= 6:
        return 6, 1
    return 1, 0


def mono_frame_from_capture(indata, capture_channels: int, channel_index: int) -> np.ndarray:
    samples = np.frombuffer(indata, dtype=np.int16)
    if capture_channels == 1:
        return samples
    if len(samples) % capture_channels != 0:
        raise ValueError("capture buffer is not whole interleaved frames")
    return samples[channel_index::capture_channels]


def frame_levels(frame: np.ndarray) -> tuple[int, int]:
    peak = int(np.max(np.abs(frame)))
    rms = int(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
    return peak, rms


def mic_level_preflight(input_device: int | None, capture_channels: int, channel_index: int) -> int:
    import sounddevice as sd

    frames = FRAME_SAMPLES * 12  # ~1 s
    print("Level check (1 s) — speak now...")
    audio = sd.rec(
        frames,
        samplerate=16000,
        channels=capture_channels,
        dtype="int16",
        device=input_device,
        blocking=True,
    )
    if capture_channels == 1:
        mono = audio[:, 0] if audio.ndim > 1 else audio.reshape(-1)
    else:
        mono = audio[:, channel_index]
    peak = int(np.max(np.abs(mono)))
    print(f"  peak={peak}  (speaking should be hundreds–thousands; ~0 means no mic signal)")
    if peak < 200:
        print(
            "  Mic looks silent. On macOS: System Settings → Privacy & Security → Microphone → "
            "enable the app running this script (Terminal / iTerm / Cursor).",
            file=sys.stderr,
        )
    return peak


def run_load_test(model_path: Path) -> str:
    model, wake_name = load_model(model_path)
    frame = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    score = model.predict(frame)[wake_name]
    print(f"Loaded {model_path.name} (key={wake_name!r})")
    print(f"Silence frame score: {score:.4f}")
    return wake_name


def run_wav_test(model_path: Path, wav_path: Path, threshold: float) -> None:
    model, wake_name = load_model(model_path)
    with wave.open(str(wav_path), "rb") as wav_file:
        if wav_file.getnchannels() != 1:
            raise ValueError("WAV must be mono")
        if wav_file.getsampwidth() != 2:
            raise ValueError("WAV must be 16-bit PCM")
        if wav_file.getframerate() != 16000:
            raise ValueError("WAV must be 16 kHz")
        pcm = wav_file.readframes(wav_file.getnframes())
    samples = np.frombuffer(pcm, dtype=np.int16)
    if len(samples) < FRAME_SAMPLES:
        raise ValueError(f"WAV too short ({len(samples)} samples); need at least {FRAME_SAMPLES}")

    print(f"Scoring {wav_path} ({len(samples) / 16000:.2f}s, key={wake_name!r}, threshold={threshold})")
    peak = 0.0
    fired_at: list[float] = []
    for start in range(0, len(samples) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        frame = samples[start : start + FRAME_SAMPLES]
        score = float(model.predict(frame)[wake_name])
        peak = max(peak, score)
        t = start / 16000
        if score >= threshold:
            fired_at.append(t)
        print(f"  {t:6.2f}s  score={score:.4f}{'  FIRE' if score >= threshold else ''}")
    print(f"Peak score: {peak:.4f}")
    if fired_at:
        print(f"Frames at/above threshold: {', '.join(f'{t:.2f}s' for t in fired_at)}")
    else:
        print("No frame reached threshold.")


def run_mic_test(model_path: Path, device: str | None, threshold: float, debounce_secs: float) -> None:
    import sounddevice as sd

    model, wake_name = load_model(model_path)
    input_device, device_name = resolve_input_device(device)
    capture_channels, channel_index = capture_channels_for_device(input_device)
    if capture_channels == 6:
        print(f"Using 6-channel capture, wake word on channel {channel_index} (ReSpeaker-style)")

    mic_level_preflight(input_device, capture_channels, channel_index)

    print(
        f"Listening on [{input_device}] {device_name} "
        f"(key={wake_name!r}, threshold={threshold}, Ctrl+C to stop)"
    )
    print("Each line: peak/rms of the frame, then model score. peak should jump when you speak.")
    last_fire = 0.0
    quiet_frames = 0
    warned_quiet = False

    def callback(indata, _frames, _time, status) -> None:
        nonlocal last_fire, quiet_frames, warned_quiet
        if status:
            print(f"  capture status: {status}", file=sys.stderr)
        frame = mono_frame_from_capture(indata, capture_channels, channel_index)
        if len(frame) != FRAME_SAMPLES:
            return
        peak, rms = frame_levels(frame)
        score = float(model.predict(frame)[wake_name])
        now = time.monotonic()
        fired = score >= threshold and (now - last_fire) >= debounce_secs
        mark = " <<<" if fired else ""
        print(f"  peak={peak:5d} rms={rms:5d}  score={score:.4f}{mark}")
        if fired:
            last_fire = now
            print(f"  ** wake detected (score={score:.4f}) **")
        if peak < 200:
            quiet_frames += 1
        else:
            quiet_frames = 0
        if not warned_quiet and quiet_frames >= 25:
            warned_quiet = True
            print(
                "  Still no mic signal (peak < 200). Check macOS Microphone permission "
                "for this terminal app, or try --list-devices and --device.",
                file=sys.stderr,
            )

    with sd.RawInputStream(
        device=input_device,
        samplerate=16000,
        blocksize=FRAME_SAMPLES,
        channels=capture_channels,
        dtype="int16",
        callback=callback,
    ):
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nStopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help=f"Custom ONNX model (default: {DEFAULT_MODEL})")
    parser.add_argument("--threshold", type=float, default=0.5, help="Score threshold for FIRE / mic detection")
    parser.add_argument("--debounce-secs", type=float, default=2.0, help="Min seconds between mic detections")
    parser.add_argument("--wav", type=Path, help="Score a 16 kHz mono 16-bit WAV file")
    parser.add_argument("--mic", action="store_true", help="Stream from microphone until Ctrl+C")
    parser.add_argument("--device", help="Substring match for sounddevice input name (e.g. XVF3800)")
    parser.add_argument("--list-devices", action="store_true", help="List input devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        list_input_devices()
        return 0

    model_path = args.model.resolve()
    if args.wav and args.mic:
        parser.error("Use either --wav or --mic, not both")
    if args.wav:
        run_wav_test(model_path, args.wav.resolve(), args.threshold)
    elif args.mic:
        run_mic_test(model_path, args.device, args.threshold, args.debounce_secs)
    else:
        run_load_test(model_path)
        print("OK — model loads and predict() runs. Try --mic or --wav for live scoring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
