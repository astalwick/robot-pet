#!/usr/bin/env python3
"""
Wake word + chime hardware test — run while robot-voice is stopped.

Uses shared ReSpeakerAudio capture/playback (Phase 0) and plays the wake chime
on detection. For model-only checks without audio I/O, use wakeword-model-smoke-test.py.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/diagnostics/wakeword-chime-test.py
    python scripts/diagnostics/wakeword-chime-test.py --threshold 0.4
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_MODEL = REPO_ROOT / "models" / "wake" / "Hey_Bloop.onnx"
DEFAULT_CHIME = REPO_ROOT / "assets" / "audio" / "wake_chime.wav"


async def run(args: argparse.Namespace) -> None:
    from drivers.respeaker import WAKE_MIC_QUEUE_SIZE, ReSpeakerAudio
    from voice.wakeword import WakeWordDetector

    model_path = args.model.resolve()
    chime_path = args.chime.resolve()
    if not chime_path.is_file():
        raise FileNotFoundError(f"Chime WAV not found: {chime_path}")

    detector = WakeWordDetector(model_path, threshold=args.threshold, debounce_secs=args.debounce_secs)
    wake_name = detector.load()
    print(f"Loaded {model_path.name} (key={wake_name!r}, threshold={args.threshold})")

    stop_event = asyncio.Event()
    audio = ReSpeakerAudio(
        input_device=args.input_device,
        output_device=args.output_device,
        capture_channel_index=args.capture_channel_index,
        input_gain=args.input_gain,
        output_gain=args.output_gain,
    )
    await audio.start_io(stop_event)
    print(
        f"Listening on {args.input_device!r} — say the wake phrase; "
        f"chime={chime_path.name}; Ctrl+C to stop"
    )

    try:
        async for frame in audio.mic_frames(
            stop_event,
            queue_size=WAKE_MIC_QUEUE_SIZE,
            warn_on_drop=False,
        ):
            if not detector.check(frame):
                continue
            score = detector.last_score
            print(f"** wake detected score={score:.4f} (fire #{detector.fire_count}) **")
            await audio.play_wav(str(chime_path))
    finally:
        stop_event.set()
        await audio.stop_io()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help=f"Wake ONNX model (default: {DEFAULT_MODEL})")
    parser.add_argument("--chime", type=Path, default=DEFAULT_CHIME, help=f"Chime WAV (default: {DEFAULT_CHIME})")
    parser.add_argument("--threshold", type=float, default=0.5, help="Score threshold for detection")
    parser.add_argument("--debounce-secs", type=float, default=2.0, help="Min seconds between detections")
    parser.add_argument("--input-device", default="XVF3800", help="ReSpeaker input device name or ALSA path")
    parser.add_argument("--output-device", default="XVF3800", help="ReSpeaker output device name or ALSA path")
    parser.add_argument("--capture-channel-index", type=int, default=1, help="Processed mic channel (ReSpeaker default: 1)")
    parser.add_argument("--input-gain", type=float, default=1.0)
    parser.add_argument("--output-gain", type=float, default=1.0)
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
