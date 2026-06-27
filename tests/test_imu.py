import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from drivers.imu import (
    average_vectors,
    average_quaternions,
    quaternion_to_euler_degrees,
    quaternion_to_rotation_vector_degrees,
    read_bno085_quaternion,
    relative_quaternion,
    vector_rotation_degrees,
)


def z_rotation(degrees):
    radians = math.radians(degrees)
    return (0.0, 0.0, math.sin(radians / 2), math.cos(radians / 2))


def y_rotation(degrees):
    radians = math.radians(degrees)
    return (0.0, math.sin(radians / 2), 0.0, math.cos(radians / 2))


class ImuMathTest(unittest.TestCase):
    def test_relative_quaternion_removes_zero_orientation(self):
        relative = relative_quaternion(z_rotation(30), z_rotation(75))

        _roll, _pitch, yaw = quaternion_to_euler_degrees(relative)

        self.assertAlmostEqual(yaw, 45.0)

    def test_quaternion_to_euler_degrees_reports_pitch(self):
        radians = math.radians(20)
        quaternion = (0.0, math.sin(radians / 2), 0.0, math.cos(radians / 2))

        _roll, pitch, _yaw = quaternion_to_euler_degrees(quaternion)

        self.assertAlmostEqual(pitch, 20.0)

    def test_average_quaternions_handles_opposite_signs(self):
        averaged = average_quaternions(
            [z_rotation(10), tuple(-value for value in z_rotation(10))]
        )

        _roll, _pitch, yaw = quaternion_to_euler_degrees(averaged)

        self.assertAlmostEqual(yaw, 10.0)

    def test_rotation_vector_keeps_pure_axis_rotation_separate(self):
        sensor_x, sensor_y, sensor_z = quaternion_to_rotation_vector_degrees(
            y_rotation(-90)
        )

        self.assertAlmostEqual(sensor_x, 0.0)
        self.assertAlmostEqual(sensor_y, -90.0)
        self.assertAlmostEqual(sensor_z, 0.0)

    def test_vector_rotation_degrees_reports_gravity_tilt_axis(self):
        zero_gravity = (0.0, -1.0, 0.0)
        current_gravity = (
            0.0,
            -math.cos(math.radians(10)),
            -math.sin(math.radians(10)),
        )

        sensor_x, sensor_y, sensor_z = vector_rotation_degrees(
            zero_gravity, current_gravity
        )

        self.assertAlmostEqual(sensor_x, 10.0)
        self.assertAlmostEqual(sensor_y, 0.0)
        self.assertAlmostEqual(sensor_z, 0.0)

    def test_average_vectors_normalizes_result(self):
        self.assertEqual(
            average_vectors([(0.0, -9.0, 0.0), (0.0, -10.0, 0.0)]),
            (0.0, -1.0, 0.0),
        )

    def test_read_bno085_quaternion_waits_for_first_report(self):
        class FakeSensor:
            def __init__(self):
                self.reads = 0

            @property
            def game_quaternion(self):
                self.reads += 1
                if self.reads == 1:
                    raise RuntimeError("No game quaternion report found")
                return z_rotation(5)

        quaternion = read_bno085_quaternion(FakeSensor(), "game")

        _roll, _pitch, yaw = quaternion_to_euler_degrees(quaternion)
        self.assertAlmostEqual(yaw, 5.0)


if __name__ == "__main__":
    unittest.main()
