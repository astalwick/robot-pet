import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.sensors import SafetyConfig, SensorEntry, SensorsConfig
from control.safety_gate import (
    Clearances,
    SafetyState,
    blocking_reason,
    cancel_blocked_qpps,
    evaluate_safety,
    is_forward_motion,
    scan_clearances,
)

LIDAR_CONFIG = SensorsConfig(
    safety=SafetyConfig(enabled=True, lidar_stop_below_mm=150),
    sensors=(SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),),
)
FLOOR_OK = [{"name": "cliff_left", "ok": True, "distance_mm": 80}]


def one_return(distance, angle):
    """A scan with a single valid return at this laser-frame angle (A1 limits)."""
    return scan_clearances([distance], angle, 0.0, 0.15, 12.0)


class SafetyGateTest(unittest.TestCase):
    def test_is_forward_motion(self):
        self.assertTrue(is_forward_motion(100, 100))
        self.assertTrue(is_forward_motion(100, -50))
        self.assertFalse(is_forward_motion(-100, 100))
        self.assertFalse(is_forward_motion(0, 0))

    def test_cliff_trip_blocks_forward(self):
        config = SensorsConfig(
            safety=SafetyConfig(enabled=True, cliff_trip_above_mm=200),
            sensors=(
                SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),
            ),
        )
        readings = [{"name": "cliff_left", "ok": True, "distance_mm": 250}]

        state = evaluate_safety(readings, config, sensors_live=True)

        self.assertTrue(state.blocked)
        self.assertEqual(state.reason, "cliff_left_cliff")

    def test_forward_obstacle_blocks_forward(self):
        config = SensorsConfig(
            safety=SafetyConfig(enabled=True, forward_stop_below_mm=150),
            sensors=(
                SensorEntry("front", "vl53l1x", 3, role="forward"),
            ),
        )
        readings = [{"name": "front", "ok": True, "distance_mm": 100}]

        state = evaluate_safety(readings, config, sensors_live=True)

        self.assertTrue(state.blocked)

    def test_failed_read_does_not_block(self):
        config = SensorsConfig(
            safety=SafetyConfig(enabled=True),
            sensors=(SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),),
        )
        readings = [{"name": "cliff_left", "ok": False, "distance_mm": None}]

        state = evaluate_safety(readings, config, sensors_live=True)

        self.assertFalse(state.blocked)

    def test_stale_sensors_block_when_safety_is_enabled(self):
        config = SensorsConfig(
            safety=SafetyConfig(enabled=True),
            sensors=(SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),),
        )

        state = evaluate_safety([], config, sensors_live=False)

        self.assertTrue(state.blocked)
        self.assertEqual(state.reason, "sensors_stale")

    def test_safety_disabled_never_blocks(self):
        config = SensorsConfig(
            safety=SafetyConfig(enabled=False),
            sensors=(SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),),
        )
        readings = [{"name": "cliff_left", "ok": True, "distance_mm": 9999}]

        state = evaluate_safety(readings, config, sensors_live=True)

        self.assertFalse(state.blocked)

    def test_blocked_safety_cancels_forward_but_preserves_rotation_and_reverse(self):
        blocked = SafetyState(blocked=True, reason="test")

        self.assertEqual(cancel_blocked_qpps(200, 200, blocked), (0, 0))
        self.assertEqual(cancel_blocked_qpps(-200, 200, blocked), (-200, 200))
        self.assertEqual(cancel_blocked_qpps(200, 0, blocked), (100, -100))
        self.assertEqual(cancel_blocked_qpps(-200, -200, blocked), (-200, -200))

    def test_reverse_block_cancels_reverse_but_preserves_rotation_and_forward(self):
        blocked = SafetyState(blocked=False, reverse_blocked=True, reverse_reason="lidar_rear_obstacle")

        self.assertEqual(cancel_blocked_qpps(-200, -200, blocked), (0, 0))
        self.assertEqual(cancel_blocked_qpps(-200, 200, blocked), (-200, 200))
        self.assertEqual(cancel_blocked_qpps(0, -200, blocked), (100, -100))
        self.assertEqual(cancel_blocked_qpps(200, 200, blocked), (200, 200))

    def test_blocking_reason_follows_the_commanded_direction(self):
        both = SafetyState(blocked=True, reason="front_center_obstacle", reverse_blocked=True, reverse_reason="lidar_rear_obstacle")

        self.assertEqual(blocking_reason(200, 200, both), "front_center_obstacle")
        self.assertEqual(blocking_reason(-200, -200, both), "lidar_rear_obstacle")
        # turning in place is never blocked
        self.assertIsNone(blocking_reason(-200, 200, both))
        self.assertIsNone(blocking_reason(0, 0, both))

    def test_scan_clearances_measures_from_the_body_edges(self):
        # the laser sits 82 mm behind the axle; the front edge is 64 mm ahead of it
        ahead = one_return(1.0, 0.0)
        self.assertAlmostEqual(ahead.forward_m, 0.854)
        self.assertIsNone(ahead.rear_m)

        # the rear edge is 266 mm behind the axle, so 184 mm behind the laser
        behind = one_return(1.0, math.pi)
        self.assertIsNone(behind.forward_m)
        self.assertAlmostEqual(behind.rear_m, 0.816)

    def test_scan_clearances_ignores_points_beside_and_inside_the_body(self):
        self.assertEqual(one_return(0.5, math.pi / 2), Clearances(None, None))
        self.assertEqual(one_return(0.5, -math.pi / 2), Clearances(None, None))
        # 0.1 m ahead of the laser is still 18 mm behind the front edge
        self.assertEqual(one_return(0.1, 0.0), Clearances(None, None))
        # out of range: no return, and short reflections off the deck
        self.assertEqual(one_return(math.inf, 0.0), Clearances(None, None))
        self.assertEqual(one_return(0.05, 0.0), Clearances(None, None))

    def test_scan_clearances_keeps_the_nearest_return_each_way(self):
        # 0, 90, 180 and 270 degrees; a nearer return straight ahead comes later
        ranges = [2.0, 0.3, 1.5, 0.3]
        clearances = scan_clearances(ranges, 0.0, math.pi / 2, 0.15, 12.0)
        self.assertAlmostEqual(clearances.forward_m, 2.0 - 0.082 - 0.064)
        self.assertAlmostEqual(clearances.rear_m, 1.5 - (0.266 - 0.082))

        ranges = [2.0, 0.3, 1.5, 0.3, 0.6]
        clearances = scan_clearances(ranges, 0.0, math.pi / 2, 0.15, 12.0)
        self.assertAlmostEqual(clearances.forward_m, 0.6 - 0.082 - 0.064)

    def test_lidar_forward_clearance_blocks_forward_only(self):
        state = evaluate_safety(
            FLOOR_OK, LIDAR_CONFIG, sensors_live=True, clearances=Clearances(0.10, 1.0), clearances_age=0.2
        )

        self.assertTrue(state.blocked)
        self.assertEqual(state.reason, "lidar_forward_obstacle")
        self.assertFalse(state.reverse_blocked)

    def test_lidar_rear_clearance_blocks_reverse_only(self):
        state = evaluate_safety(
            FLOOR_OK, LIDAR_CONFIG, sensors_live=True, clearances=Clearances(1.0, 0.10), clearances_age=0.2
        )

        self.assertFalse(state.blocked)
        self.assertTrue(state.reverse_blocked)
        self.assertEqual(state.reverse_reason, "lidar_rear_obstacle")

    def test_lidar_rule_is_off_without_a_fresh_scan(self):
        close = Clearances(0.10, 0.10)

        stale = evaluate_safety(FLOOR_OK, LIDAR_CONFIG, sensors_live=True, clearances=close, clearances_age=1.5)
        self.assertEqual(stale, SafetyState(blocked=False))

        none = evaluate_safety(FLOOR_OK, LIDAR_CONFIG, sensors_live=True)
        self.assertEqual(none, SafetyState(blocked=False))

        clear = evaluate_safety(FLOOR_OK, LIDAR_CONFIG, sensors_live=True, clearances=Clearances(None, None), clearances_age=0.1)
        self.assertEqual(clear, SafetyState(blocked=False))

    def test_barrier_ahead_blocks_forward_only_inside_the_stop_distance(self):
        state = evaluate_safety(FLOOR_OK, LIDAR_CONFIG, sensors_live=True, barriers=Clearances(0.15, 1.0), barriers_age=0.1)
        self.assertTrue(state.blocked)
        self.assertEqual(state.reason, "barrier_ahead")
        self.assertFalse(state.reverse_blocked)

        room = evaluate_safety(FLOOR_OK, LIDAR_CONFIG, sensors_live=True, barriers=Clearances(0.25, 1.0), barriers_age=0.1)
        self.assertEqual(room, SafetyState(blocked=False))

    def test_barrier_behind_blocks_reverse_only(self):
        state = evaluate_safety(FLOOR_OK, LIDAR_CONFIG, sensors_live=True, barriers=Clearances(None, 0.1), barriers_age=0.1)
        self.assertFalse(state.blocked)
        self.assertEqual(state.reverse_reason, "barrier_behind")

    def test_barrier_rule_lapses_without_a_fresh_pose(self):
        stale = evaluate_safety(FLOOR_OK, LIDAR_CONFIG, sensors_live=True, barriers=Clearances(0.0, 0.0), barriers_age=1.5)
        self.assertEqual(stale, SafetyState(blocked=False))

    def test_lidar_reasons_win_over_barrier_reasons(self):
        state = evaluate_safety(
            FLOOR_OK,
            LIDAR_CONFIG,
            sensors_live=True,
            clearances=Clearances(0.10, 0.10),
            clearances_age=0.2,
            barriers=Clearances(0.0, 0.0),
            barriers_age=0.1,
        )
        self.assertEqual(state.reason, "lidar_forward_obstacle")
        self.assertEqual(state.reverse_reason, "lidar_rear_obstacle")

    def test_tof_trip_wins_the_forward_reason_over_the_lidar(self):
        config = SensorsConfig(
            safety=SafetyConfig(enabled=True, forward_stop_below_mm=150),
            sensors=(SensorEntry("front", "vl53l1x", 3, role="forward"),),
        )
        readings = [{"name": "front", "ok": True, "distance_mm": 100}]

        state = evaluate_safety(readings, config, sensors_live=True, clearances=Clearances(0.05, 0.05), clearances_age=0.1)

        self.assertEqual(state.reason, "front_obstacle")
        self.assertTrue(state.reverse_blocked)

    def test_stale_tofs_still_let_the_lidar_guard_reverse(self):
        state = evaluate_safety([], LIDAR_CONFIG, sensors_live=False, clearances=Clearances(1.0, 0.05), clearances_age=0.1)

        self.assertEqual(state.reason, "sensors_stale")
        self.assertTrue(state.reverse_blocked)


if __name__ == "__main__":
    unittest.main()
