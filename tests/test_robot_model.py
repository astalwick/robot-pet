import math
import unittest
from pathlib import Path

from robot_model import (
    ENCODER_COUNTS_PER_METER,
    ENCODER_COUNTS_PER_WHEEL_REVOLUTION,
    FOOTPRINT_FRONT_X_METERS,
    FOOTPRINT_HALF_WIDTH_METERS,
    FOOTPRINT_REAR_X_METERS,
    SENSOR_MOUNTS,
    WHEEL_DIAMETER_METERS,
)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class RobotModelTest(unittest.TestCase):
    def test_encoder_counts_per_meter_matches_inputs(self):
        expected = ENCODER_COUNTS_PER_WHEEL_REVOLUTION / (math.pi * WHEEL_DIAMETER_METERS)
        self.assertTrue(math.isclose(ENCODER_COUNTS_PER_METER, expected))

    def test_sensor_names_are_unique(self):
        names = [mount.name for mount in SENSOR_MOUNTS]
        self.assertEqual(len(names), len(set(names)))

    def test_rep103_left_is_positive_y(self):
        mounts = {mount.name: mount for mount in SENSOR_MOUNTS}
        self.assertGreater(mounts["forward_left"].y, 0)
        self.assertLess(mounts["forward_right"].y, 0)
        self.assertGreater(mounts["cliff_left"].y, 0)
        self.assertLess(mounts["cliff_right"].y, 0)

    def test_nav2_costmaps_use_the_robot_footprint(self):
        # PyYAML is not installed on the dev machines, so match the text.
        front, rear, half = FOOTPRINT_FRONT_X_METERS, FOOTPRINT_REAR_X_METERS, FOOTPRINT_HALF_WIDTH_METERS
        footprint = f'footprint: "[[{front}, {half}], [{front}, -{half}], [{rear}, -{half}], [{rear}, {half}]]"'
        nav2 = (CONFIG_DIR / "nav2.yaml").read_text()
        # both costmaps: the planner and the controller agree on what fits
        self.assertEqual(nav2.count(footprint), 2, footprint)
        self.assertNotIn("robot_radius", nav2)
        # Both costmaps take the barriers from the one costmap_filter_info_server.
        self.assertEqual(nav2.count('filters: ["keepout_filter"]'), 2)
        self.assertEqual(nav2.count('filter_info_topic: "/costmap_filter_info"'), 3)
        self.assertEqual(nav2.count('"/keepout_mask"'), 2)
        # Our motion node speaks plain Twist; both cmd_vel publishers must agree.
        self.assertEqual(nav2.count("enable_stamped_cmd_vel: false"), 2)
        # AMCL's own recovery is off; the nav bridge's search finds a carried robot
        self.assertIn("recovery_alpha_fast: 0.0", nav2)
        self.assertIn("recovery_alpha_slow: 0.0", nav2)
        # AMCL corrects every 10 cm or 6 degrees with a sharp beam weight on our 5 cm map
        self.assertIn("update_min_d: 0.1", nav2)
        self.assertIn("update_min_a: 0.1", nav2)
        self.assertIn("sigma_hit: 0.1", nav2)
        self.assertIn("/nav_lock", (CONFIG_DIR / "twist_mux.yaml").read_text())

    def test_cliff_sensors_pitch_downward(self):
        for mount in SENSOR_MOUNTS:
            if mount.name.startswith("cliff"):
                # Positive pitch points the beam below horizontal in a z-up frame.
                self.assertGreater(mount.pitch, 0)
            else:
                self.assertEqual(mount.pitch, 0)


if __name__ == "__main__":
    unittest.main()
