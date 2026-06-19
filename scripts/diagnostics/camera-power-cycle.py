#!/usr/bin/env python3
"""
Measure the cost of power-cycling the camera: cold-start to a usable frame, and
snapshot to fully released.

This drives the real CameraDriver with the same defaults robot-camera uses, so
the numbers reflect the path we'd actually ship for an idle "wake, snap, sleep"
mode. Stop robot-camera first so nothing else owns the sensor.

Per cycle it reports three times:
  - first_frame_ms: driver.start() until the first JPEG is delivered (pipeline
    bring-up).
  - settled_ms:     until lores-frame brightness stops changing (a proxy for
    auto-exposure converging — i.e. the first frame that's actually usable, not
    just the first frame out). DARK ROOMS fool this: a flat-black frame looks
    "settled" instantly, so always eyeball the saved JPEGs too.
  - stop_ms:        driver.stop() until it returns (teardown).

Sample frames are saved to --out as cycleN-first.jpg / cycleN-settled.jpg so you
can judge for yourself whether "settled" is good enough for face detection.

Usage:
    cd ~/robot-pet
    source .venv/bin/activate
    sudo systemctl stop robot-camera
    python scripts/diagnostics/camera-power-cycle.py
    python scripts/diagnostics/camera-power-cycle.py --cycles 10 --cooldown 10
    python scripts/diagnostics/camera-power-cycle.py --verbose   # dump brightness curve
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

if not sys.platform.startswith("linux"):
    print("This script must run on the Pi (Linux + libcamera).")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from drivers.camera import CameraDriver, CameraUnavailable


class CycleResult:
    def __init__(self) -> None:
        self.first_frame_ms: float | None = None
        self.settled_ms: float | None = None
        self.stop_ms: float | None = None
        self.settled: bool = False


def run_cycle(fps: float, settle_window: int, settle_threshold: float,
              max_settle: float, out_dir: Path | None, index: int,
              verbose: bool) -> CycleResult:
    """Start the camera cold, wait for a settled frame, stop it. All times in ms."""
    driver = CameraDriver(fps=fps)
    result = CycleResult()

    lock = threading.Lock()
    state = {"first_jpeg": None, "latest_jpeg": None}
    brightness: list[tuple[float, float]] = []  # (t_ms, mean_luma)

    def on_jpeg(frame: bytes) -> None:
        now = (time.perf_counter() - t0) * 1000.0
        with lock:
            if state["first_jpeg"] is None:
                state["first_jpeg"] = frame
                result.first_frame_ms = now
            state["latest_jpeg"] = frame

    def on_lores(gray: bytes, width: int, height: int) -> None:
        now = (time.perf_counter() - t0) * 1000.0
        mean_luma = sum(gray) / len(gray) if gray else 0.0
        with lock:
            brightness.append((now, mean_luma))

    t0 = time.perf_counter()
    driver.start(on_jpeg, on_lores)

    # Watch the brightness curve until it flattens (AE converged) or we time out.
    deadline = t0 + max_settle
    while time.perf_counter() < deadline:
        with lock:
            recent = [luma for _, luma in brightness[-settle_window:]]
            last_t = brightness[-1][0] if brightness else None
        if len(recent) >= settle_window:
            mean = max(statistics.fmean(recent), 1.0)
            if (max(recent) - min(recent)) / mean <= settle_threshold:
                result.settled_ms = last_t
                result.settled = True
                break
        time.sleep(0.02)

    with lock:
        first_jpeg = state["first_jpeg"]
        settled_jpeg = state["latest_jpeg"]
        curve = list(brightness)

    stop_start = time.perf_counter()
    driver.stop()
    result.stop_ms = (time.perf_counter() - stop_start) * 1000.0

    if out_dir is not None:
        if first_jpeg is not None:
            (out_dir / f"cycle{index}-first.jpg").write_bytes(first_jpeg)
        if settled_jpeg is not None:
            (out_dir / f"cycle{index}-settled.jpg").write_bytes(settled_jpeg)

    if verbose:
        print("  brightness curve (t_ms: mean_luma):")
        for t_ms, luma in curve:
            print(f"    {t_ms:7.1f}: {luma:6.1f}")

    return result


def summarize(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label:14s}  (no data)")
        return
    print(f"  {label:14s}  median {statistics.median(values):7.1f} ms   "
          f"min {min(values):7.1f}   max {max(values):7.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Time camera cold-start and shutdown.")
    parser.add_argument("--cycles", type=int, default=5, help="Number of power cycles to run.")
    parser.add_argument("--cooldown", type=float, default=5.0,
                        help="Seconds the camera stays off between cycles (let it cool to a true cold start).")
    parser.add_argument("--fps", type=float, default=10.0, help="Capture FPS (matches robot-camera default).")
    parser.add_argument("--settle-window", type=int, default=5,
                        help="Consecutive lores frames that must be stable to call exposure 'settled'.")
    parser.add_argument("--settle-threshold", type=float, default=0.02,
                        help="Max (max-min)/mean brightness spread across the window to count as stable.")
    parser.add_argument("--max-settle", type=float, default=4.0,
                        help="Give up waiting for settle after this many seconds.")
    parser.add_argument("--out", default="/tmp/camera-power-cycle",
                        help="Directory for sample JPEGs (set empty to skip saving).")
    parser.add_argument("--verbose", action="store_true", help="Print the brightness curve for each cycle.")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Power-cycling the camera {args.cycles}x "
          f"({args.cooldown:.0f}s cooldown between cycles, {args.fps:.0f} fps)\n")

    results: list[CycleResult] = []
    for index in range(1, args.cycles + 1):
        try:
            result = run_cycle(args.fps, args.settle_window, args.settle_threshold,
                               args.max_settle, out_dir, index, args.verbose)
        except CameraUnavailable as exc:
            print(f"camera unavailable: {exc}")
            print("Is robot-camera still running and holding the sensor? Stop it and retry.")
            sys.exit(1)

        settle_note = f"{result.settled_ms:7.1f} ms" if result.settled else "  NOT SETTLED"
        print(f"cycle {index:2d}:  first_frame {result.first_frame_ms or float('nan'):7.1f} ms"
              f"   settled {settle_note}"
              f"   stop {result.stop_ms:6.1f} ms")
        results.append(result)

        if index < args.cycles:
            time.sleep(args.cooldown)

    print("\nsummary:")
    summarize("first_frame", [r.first_frame_ms for r in results if r.first_frame_ms is not None])
    summarize("settled", [r.settled_ms for r in results if r.settled])
    summarize("stop", [r.stop_ms for r in results if r.stop_ms is not None])

    settled_count = sum(1 for r in results if r.settled)
    if settled_count < len(results):
        print(f"\n  note: {len(results) - settled_count}/{len(results)} cycles never settled within "
              f"{args.max_settle:.0f}s — raise --max-settle or check the saved frames.")
    if out_dir is not None:
        print(f"\n  sample frames in {out_dir} — eyeball cycleN-first vs cycleN-settled "
              f"to confirm 'settled' is actually usable.")


if __name__ == "__main__":
    main()
