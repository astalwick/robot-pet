import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from control.odometry import DiffDriveOdometry, Pose


TRACK_WIDTH = 0.306


class DiffDriveOdometryTest(unittest.TestCase):
    def test_straight_line_advances_x_only(self):
        odom = DiffDriveOdometry(TRACK_WIDTH)

        pose = odom.update(0.5, 0.5)

        self.assertAlmostEqual(pose.x, 0.5)
        self.assertAlmostEqual(pose.y, 0.0)
        self.assertAlmostEqual(pose.theta, 0.0)

    def test_left_turn_in_place_increases_theta(self):
        odom = DiffDriveOdometry(TRACK_WIDTH)
        d = 0.02

        pose = odom.update(-d, d)

        # Right wheel ahead of left => CCW => positive theta.
        self.assertGreater(pose.theta, 0.0)
        self.assertAlmostEqual(pose.theta, 2 * d / TRACK_WIDTH)
        self.assertAlmostEqual(pose.x, 0.0)
        self.assertAlmostEqual(pose.y, 0.0)

    def test_right_turn_in_place_decreases_theta(self):
        odom = DiffDriveOdometry(TRACK_WIDTH)
        d = 0.02

        pose = odom.update(d, -d)

        self.assertLess(pose.theta, 0.0)
        self.assertAlmostEqual(pose.theta, -2 * d / TRACK_WIDTH)
        self.assertAlmostEqual(pose.x, 0.0)
        self.assertAlmostEqual(pose.y, 0.0)

    def test_quarter_circle_arc_lands_near_analytic_endpoint(self):
        # Left wheel stationary, right wheel drives: the base pivots about the left
        # wheel (at +track_width/2). A quarter turn (theta = pi/2) about that pivot
        # lands the center at (w/2, w/2) facing +y. Splitting the travel into many
        # small steps makes the midpoint integration converge to that analytic
        # endpoint; 1000 steps holds it to ~1e-4 m, so 1e-3 is a comfortable margin.
        odom = DiffDriveOdometry(TRACK_WIDTH)
        steps = 1000
        right_total = TRACK_WIDTH * (math.pi / 2)  # d_theta = right/track = pi/2
        step = right_total / steps

        for _ in range(steps):
            pose = odom.update(0.0, step)

        self.assertAlmostEqual(pose.theta, math.pi / 2, places=6)
        self.assertAlmostEqual(pose.x, TRACK_WIDTH / 2, delta=1e-3)
        self.assertAlmostEqual(pose.y, TRACK_WIDTH / 2, delta=1e-3)

    def test_theta_normalizes_into_half_open_interval(self):
        odom = DiffDriveOdometry(TRACK_WIDTH)
        # Turn left past pi in a single step, then confirm the wrap.
        d_theta = math.pi * 1.5
        wheel = d_theta * TRACK_WIDTH / 2

        pose = odom.update(-wheel, wheel)

        self.assertGreater(pose.theta, -math.pi)
        self.assertLessEqual(pose.theta, math.pi)
        self.assertAlmostEqual(pose.theta, -math.pi / 2)

    def test_reset_returns_to_origin(self):
        odom = DiffDriveOdometry(TRACK_WIDTH)
        odom.update(0.3, 0.1)

        odom.reset()
        pose = odom.update(0.0, 0.0)

        self.assertEqual(pose, Pose(0.0, 0.0, 0.0))

    def test_pose_property_matches_last_update_and_is_a_copy(self):
        odom = DiffDriveOdometry(TRACK_WIDTH)
        returned = odom.update(0.5, 0.5)

        self.assertEqual(odom.pose, returned)

        odom.pose.x = 999.0  # mutating the copy must not disturb internal state
        self.assertAlmostEqual(odom.pose.x, 0.5)

    def test_returned_pose_is_a_copy(self):
        odom = DiffDriveOdometry(TRACK_WIDTH)

        pose = odom.update(0.5, 0.5)
        pose.x = 999.0
        pose.y = 999.0
        pose.theta = 999.0

        # Mutating the returned pose must not disturb internal state.
        next_pose = odom.update(0.5, 0.5)
        self.assertAlmostEqual(next_pose.x, 1.0)
        self.assertAlmostEqual(next_pose.y, 0.0)
        self.assertAlmostEqual(next_pose.theta, 0.0)


if __name__ == "__main__":
    unittest.main()
