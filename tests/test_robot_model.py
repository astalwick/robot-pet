import math
import unittest

from robot_model import (
    ENCODER_COUNTS_PER_METER,
    ENCODER_COUNTS_PER_WHEEL_REVOLUTION,
    SENSOR_MOUNTS,
    WHEEL_DIAMETER_METERS,
)


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

    def test_cliff_sensors_pitch_downward(self):
        for mount in SENSOR_MOUNTS:
            if mount.name.startswith("cliff"):
                # Positive pitch points the beam below horizontal in a z-up frame.
                self.assertGreater(mount.pitch, 0)
            else:
                self.assertEqual(mount.pitch, 0)


if __name__ == "__main__":
    unittest.main()
