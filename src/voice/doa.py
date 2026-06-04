"""Stable direction-of-arrival tracking for the `face_me` behavior.

This owns the sample history and stable-angle cache. It does not touch USB;
`drivers.respeaker.ReSpeakerDoA` remains the hardware boundary. Readings are fed
in with an injected monotonic timestamp so the logic is fully testable without
sleeping or real hardware.
"""

from __future__ import annotations

from drivers.respeaker import DoAReading

ROBOT_FRONT_RAW_DEGREES = 270
STABILITY_DURATION_SECONDS = 0.5
STABILITY_TOLERANCE_DEGREES = 5
# Generous enough to survive the round-trip from the user finishing their
# sentence, through speech-to-text end-of-utterance + commit, to the assistant
# deciding to call `face_me`. A tight window (a couple seconds) always expired
# before the tool ran. Still short enough to reject a speaker who spoke once and
# walked away.
STABLE_CACHE_MAX_AGE_SECONDS = 10.0
ALREADY_FACING_TOLERANCE_DEGREES = 15


def circular_distance(a: float, b: float) -> float:
    """Shortest distance between two angles in degrees, so 359 and 1 are 2."""
    gap = abs(a - b) % 360
    return min(gap, 360 - gap)


def to_relative_degrees(raw_doa: int) -> int:
    """Signed robot-relative angle. Positive turns toward the left drive wheel."""
    return ((raw_doa - ROBOT_FRONT_RAW_DEGREES + 180) % 360) - 180


class DoATracker:
    def __init__(self) -> None:
        # Candidate samples are (angle, timestamp), all within tolerance of the
        # oldest one. We anchor on the oldest so a slow drift never accumulates
        # past tolerance and gets mistaken for a stable direction.
        self._candidate: list[tuple[int, float]] = []
        self.stable_angle: int | None = None
        self.stable_at = 0.0

    def update(self, reading: DoAReading, now: float, assistant_speaking: bool) -> None:
        if assistant_speaking or not reading.speech_detected:
            self._candidate = []
            return

        anchor = self._candidate[0][0] if self._candidate else None
        if anchor is None or circular_distance(reading.angle_degrees, anchor) > STABILITY_TOLERANCE_DEGREES:
            self._candidate = [(reading.angle_degrees, now)]
            return

        self._candidate.append((reading.angle_degrees, now))
        if now - self._candidate[0][1] >= STABILITY_DURATION_SECONDS:
            self.stable_angle = reading.angle_degrees
            self.stable_at = now

    def age(self, now: float) -> float | None:
        if self.stable_angle is None:
            return None
        return now - self.stable_at
