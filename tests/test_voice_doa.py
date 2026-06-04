import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.respeaker import DoAReading
from voice.doa import (
    DoATracker,
    circular_distance,
    to_relative_degrees,
)


def speech(angle):
    return DoAReading(angle, True)


def feed_stable(tracker, angle, *, start=0.0, count=6, step=0.1):
    for index in range(count):
        tracker.update(speech(angle), start + index * step, assistant_speaking=False)


class DoAConversionTest(unittest.TestCase):
    def test_front_converts_to_zero(self):
        self.assertEqual(to_relative_degrees(270), 0)

    def test_left_converts_to_positive_ninety(self):
        self.assertEqual(to_relative_degrees(0), 90)

    def test_right_converts_to_negative_ninety(self):
        self.assertEqual(to_relative_degrees(180), -90)

    def test_rear_left_converts_to_about_positive_one_seventy_four(self):
        self.assertEqual(to_relative_degrees(84), 174)

    def test_circular_distance_wraps(self):
        self.assertEqual(circular_distance(359, 1), 2)
        self.assertEqual(circular_distance(10, 200), 170)


class DoATrackerTest(unittest.TestCase):
    def test_silence_readings_do_not_become_candidates(self):
        tracker = DoATracker()
        for index in range(6):
            tracker.update(DoAReading(270, False), index * 0.1, assistant_speaking=False)
        self.assertIsNone(tracker.stable_angle)

    def test_silence_clears_in_progress_candidate(self):
        tracker = DoATracker()
        tracker.update(speech(270), 0.0, assistant_speaking=False)
        tracker.update(speech(270), 0.1, assistant_speaking=False)
        tracker.update(DoAReading(270, False), 0.2, assistant_speaking=False)
        # Candidate was cleared, so resuming speech must restart the timer and
        # not immediately accept on the next in-tolerance reading.
        tracker.update(speech(270), 0.3, assistant_speaking=False)
        self.assertIsNone(tracker.stable_angle)

    def test_assistant_playback_clears_candidate_and_cannot_cache(self):
        tracker = DoATracker()
        tracker.update(speech(270), 0.0, assistant_speaking=False)
        tracker.update(speech(270), 0.1, assistant_speaking=False)
        # Half a second of "speech" while the robot is talking must not cache.
        for index in range(6):
            tracker.update(speech(270), 0.2 + index * 0.1, assistant_speaking=True)
        self.assertIsNone(tracker.stable_angle)

    def test_unstable_samples_do_not_cache(self):
        tracker = DoATracker()
        for index, angle in enumerate((10, 40, 80, 120, 200, 300)):
            tracker.update(speech(angle), index * 0.1, assistant_speaking=False)
        self.assertIsNone(tracker.stable_angle)

    def test_drift_past_tolerance_does_not_cache(self):
        # Each step is within 5 of the previous but drifts far from the anchor.
        tracker = DoATracker()
        for index, angle in enumerate((100, 104, 108, 112, 116, 120)):
            tracker.update(speech(angle), index * 0.1, assistant_speaking=False)
        self.assertIsNone(tracker.stable_angle)

    def test_half_second_of_stable_speech_caches(self):
        tracker = DoATracker()
        feed_stable(tracker, 84)
        self.assertEqual(tracker.stable_angle, 84)

    def test_stable_cache_survives_silence(self):
        tracker = DoATracker()
        feed_stable(tracker, 84)
        tracker.update(DoAReading(84, False), 0.6, assistant_speaking=False)
        self.assertEqual(tracker.stable_angle, 84)

    def test_cache_age_available_for_stale_checks(self):
        tracker = DoATracker()
        self.assertIsNone(tracker.age(1.0))
        feed_stable(tracker, 84)
        self.assertAlmostEqual(tracker.age(3.5), 3.0)


if __name__ == "__main__":
    unittest.main()
