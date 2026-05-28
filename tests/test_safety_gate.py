import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from config.sensors import SafetyConfig, SensorEntry, SensorsConfig
from control.safety_gate import SafetyState, apply_safety_to_qpps, evaluate_safety, is_forward_motion


class SafetyGateTest(unittest.TestCase):
    def test_is_forward_motion(self):
        self.assertTrue(is_forward_motion(100, 100))
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

    def test_safety_disabled_never_blocks(self):
        config = SensorsConfig(
            safety=SafetyConfig(enabled=False),
            sensors=(SensorEntry("cliff_left", "vl53l0x", 0, role="cliff"),),
        )
        readings = [{"name": "cliff_left", "ok": True, "distance_mm": 9999}]

        state = evaluate_safety(readings, config, sensors_live=True)

        self.assertFalse(state.blocked)

    def test_apply_safety_clamps_each_forward_wheel(self):
        blocked = SafetyState(blocked=True, reason="test")

        left, right = apply_safety_to_qpps(200, 200, blocked)

        self.assertEqual((left, right), (0, 0))

        left, right = apply_safety_to_qpps(-200, 200, blocked)

        self.assertEqual((left, right), (-200, 0))

        left, right = apply_safety_to_qpps(-200, -200, blocked)

        self.assertEqual((left, right), (-200, -200))


if __name__ == "__main__":
    unittest.main()
