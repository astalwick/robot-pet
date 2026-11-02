import math
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np

import map_learner
from config.localization import LocalizationSettings


# The bridge imports ROS, which is not installed here. Stand-ins are enough:
# these tests drive the trip methods directly and never spin a node.
class Node:
    pass


ros_modules = {
    name: mock.MagicMock()
    for name in (
        "rclpy", "rclpy.action", "rclpy.duration", "rclpy.executors", "rclpy.node", "rclpy.qos", "rclpy.time",
        "action_msgs", "action_msgs.msg", "action_msgs.srv", "geometry_msgs", "geometry_msgs.msg",
        "lifecycle_msgs", "lifecycle_msgs.srv", "nav2_msgs", "nav2_msgs.action", "nav2_msgs.srv",
        "nav_msgs", "nav_msgs.msg", "sensor_msgs", "sensor_msgs.msg", "std_msgs", "std_msgs.msg", "tf2_ros",
    )
}
ros_modules["rclpy.node"].Node = Node
ros_modules["action_msgs.msg"].GoalStatus = types.SimpleNamespace(STATUS_SUCCEEDED=4, STATUS_CANCELED=5)
# Only the stand-ins come out again: the real modules the bridge pulls in
# (NumPy among them) cannot be loaded a second time.
sys.modules.update(ros_modules)
from ros_nodes import nav_node
for name in ros_modules:
    del sys.modules[name]

PLACES = {
    "kitchen": {"map": "downstairs", "x": 2.0, "y": 1.0, "yaw": 0.0},
    "hall": {"map": "downstairs", "x": -1.0, "y": 0.5, "yaw": 1.5},
    "bedroom": {"map": "upstairs", "x": 3.0, "y": 3.0, "yaw": 0.0},
}


def done(value):
    return mock.Mock(result=mock.Mock(return_value=value))


class TripTest(unittest.TestCase):
    def setUp(self):
        log_patch = mock.patch.object(nav_node, "log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

        # only what the trip methods touch, as __init__ would set it
        node = object.__new__(nav_node.NavNode)
        node.settings = LocalizationSettings()
        node.mode, node.map_name = "localize", "downstairs"
        node.places = PLACES
        node.pose = {"x": 0.0, "y": 0.0, "yaw": 0.0}
        node.pose_state, node.clean = "tracking", True
        node.unplaced_since = None
        node.last_scan_time = time.monotonic()
        node.activity, node.handled = None, None
        node.busy = threading.Lock()
        node.pose_lock = threading.Lock()
        node.nav_lock = False
        node.nav2_state = "active"
        node.nav_client = mock.Mock()
        node.get_clock = mock.Mock()
        node.trip_lock = threading.RLock()
        node.goal = None
        node.held_since = node.held_before_s = 0.0
        node.goal_attempt = 0
        node.goal_handle = None
        self.node = node
        self.handle = mock.Mock(accepted=True)

    def drive(self, place="kitchen"):
        """Ask for a trip and let it leave, with Nav2 accepting it."""
        self.node.navigate_to({"place": place})
        self.node.send_pending_goal()
        self.node.on_goal_response(self.node.goal_attempt, done(self.handle))

    def test_a_trip_waits_for_the_position_then_leaves(self):
        self.node.pose_state, self.node.clean = "searching", False
        reply = self.node.navigate_to({"place": "kitchen"})
        self.assertTrue(reply["accepted"])
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["state"], "held")
        self.assertEqual(self.node.goal["waiting_for"], "position")
        self.node.nav_client.send_goal_async.assert_not_called()

        self.node.pose_state, self.node.clean = "tracking", True
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["state"], "navigating")
        self.assertNotIn("waiting_for", self.node.goal)
        self.node.nav_client.send_goal_async.assert_called_once()

    def test_a_held_trip_resumes_as_a_new_attempt_and_the_old_result_ends_nothing(self):
        self.drive()
        self.node.hold_goal("lost")
        self.assertEqual(self.node.goal["state"], "held")
        self.handle.cancel_goal_async.assert_called_once()

        self.node.send_pending_goal()
        self.assertEqual(self.node.goal_attempt, 2)
        self.node.on_goal_result(1, done(mock.Mock(status=5)))
        self.assertEqual(self.node.goal["state"], "navigating")

        self.node.on_goal_result(2, done(mock.Mock(status=4)))
        self.assertEqual(self.node.goal["state"], "arrived")

    def test_a_trip_fails_after_hold_s_of_holding_in_all(self):
        self.node.pose_state, self.node.clean = "searching", False
        self.node.navigate_to({"place": "kitchen"})
        # held 25 s on an earlier stop, and 2 s now: still waiting
        self.node.held_before_s = 25.0
        self.node.held_since = time.monotonic() - 2.0
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["state"], "held")

        self.node.held_since = time.monotonic() - 10.0
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["state"], "failed")
        self.assertEqual(self.node.goal["reason"], "lost")

    def test_time_spent_switching_maps_counts_as_holding(self):
        self.node.pose_state, self.node.clean = "searching", False
        self.node.navigate_to({"place": "kitchen"})
        self.node.held_since = time.monotonic() - 40.0
        self.node.activity = "switching map"
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["state"], "held")

        self.node.activity = None
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["state"], "failed")

    def test_the_off_map_clock_starts_over_when_the_lidar_stops(self):
        self.node.pose_state, self.node.clean = "searching", False
        self.node.unplaced_since = time.monotonic() - 60.0
        self.assertEqual(self.node.state(), "off_map")

        self.node.last_scan_time = time.monotonic() - 5.0
        self.node.update_state()
        self.node.last_scan_time = time.monotonic()
        self.assertEqual(self.node.state(), "searching")

    def test_asking_again_for_the_same_place_keeps_the_trip(self):
        self.drive()
        goal = self.node.goal
        reply = self.node.navigate_to({"place": "kitchen"})
        self.assertIs(reply["goal"], goal)
        self.assertEqual(goal["state"], "navigating")
        self.handle.cancel_goal_async.assert_not_called()

    def test_another_place_replaces_the_trip(self):
        self.drive()
        old_goal = self.node.goal
        self.node.navigate_to({"place": "hall"})
        self.assertEqual(old_goal["state"], "cancelled")
        self.assertEqual(old_goal["reason"], "replaced")
        self.handle.cancel_goal_async.assert_called_once()
        self.assertEqual(self.node.goal["target"], "hall")
        self.assertEqual(self.node.goal["state"], "held")

    def test_a_place_on_another_map_is_refused_when_the_robot_is_sure_where_it_is(self):
        reply = self.node.navigate_to({"place": "bedroom"})
        self.assertEqual(reply, {"ok": False, "error": "place_on_other_map", "map": "upstairs"})

    def test_a_place_on_another_map_waits_while_the_robot_is_unsure(self):
        # carried upstairs: it may yet find itself on the bedroom's map
        self.node.pose_state, self.node.clean = "searching", False
        reply = self.node.navigate_to({"place": "bedroom"})
        self.assertTrue(reply["accepted"])
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["waiting_for"], "position")

        # but it turned out to be downstairs after all
        self.node.pose_state, self.node.clean = "tracking", True
        self.node.send_pending_goal()
        self.assertEqual(self.node.goal["state"], "failed")
        self.assertEqual(self.node.goal["reason"], "place_on_other_map")

    def test_a_new_pose_holds_a_driving_trip(self):
        self.drive()
        self.node.lookup_pose = mock.Mock(return_value=None)
        self.node.initial_pose_publisher = mock.Mock()
        self.node.set_initial_pose({"x": 1.0, "y": 1.0, "yaw": 0.0})
        self.assertEqual(self.node.goal["state"], "held")
        self.assertEqual(self.node.pose_state, "checking")

    def test_the_tick_cannot_send_a_trip_on_the_old_pose_mid_reseed(self):
        self.drive()
        self.node.lookup_pose = mock.Mock(return_value=None)
        tick = threading.Thread(target=self.node.send_pending_goal)

        def publish(message):
            # the tick runs while the new pose is half handed over
            tick.start()
            tick.join(0.1)

        self.node.initial_pose_publisher = mock.Mock(publish=publish)
        self.node.set_initial_pose({"x": 1.0, "y": 1.0, "yaw": 0.0})
        tick.join()
        self.assertEqual(self.node.goal["state"], "held")
        self.assertEqual(self.node.goal_attempt, 1)

    def test_a_stop_while_a_trip_request_waits_refuses_it(self):
        self.drive()
        replies = []
        request = threading.Thread(target=lambda: replies.append(self.node.navigate_to({"place": "hall"})))
        with self.node.trip_lock:
            # the request waits for the trip lock while the stop comes in
            request.start()
            request.join(0.1)
            self.node.on_nav_lock(mock.Mock(data=True))
        request.join()
        self.assertEqual(replies[0]["reason"], "wheels_busy")
        self.assertEqual(self.node.goal["target"], "kitchen")
        self.assertEqual(self.node.goal["reason"], "stopped")

    def test_a_map_switch_holds_the_trip_before_the_map_changes(self):
        self.drive()
        self.node.map_rendered = None
        states = []
        self.node.load_map = mock.Mock(side_effect=lambda name: states.append(self.node.goal["state"]) or False)
        self.node.switch_map({"map": "upstairs"}, None)
        self.assertEqual(states, ["held"])

    def test_the_gamepad_cancels_a_driving_trip_but_not_a_held_one(self):
        push = mock.Mock(linear=mock.Mock(x=0.1), angular=mock.Mock(z=0.0))
        self.node.pose_state, self.node.clean = "searching", False
        self.node.navigate_to({"place": "kitchen"})
        self.node.on_teleop(push)
        self.assertEqual(self.node.goal["state"], "held")

        self.node.pose_state, self.node.clean = "tracking", True
        self.node.send_pending_goal()
        self.node.on_teleop(push)
        self.assertEqual(self.node.goal["state"], "cancelled")
        self.assertEqual(self.node.goal["reason"], "gamepad")

    def test_with_resume_trips_off_a_stop_ends_the_trip(self):
        self.node.settings = LocalizationSettings(resume_trips=False)
        self.drive()
        self.node.hold_goal("lost")
        self.assertEqual(self.node.goal["state"], "cancelled")
        self.assertEqual(self.node.goal["reason"], "lost")



class LearnerTest(unittest.TestCase):
    """The learner in the bridge: what it learns from, and when a visit closes."""

    def setUp(self):
        log_patch = mock.patch.object(nav_node, "log")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.learned_dir = os.path.join(directory.name, "learned")
        # a 4 m x 2 m room at 5 cm with a wall along x = 3 m
        grid = np.zeros((40, 80), dtype=np.int8)
        grid[:, 60] = 100
        map_learner.write_map_file(grid.ravel(), {"width": 80, "height": 40, "resolution": 0.05, "origin_x": 0.0, "origin_y": 0.0}, os.path.join(directory.name, "downstairs"))

        # only what the learner touches, as __init__ would set it
        node = object.__new__(nav_node.NavNode)
        node.args = types.SimpleNamespace(maps_dir=directory.name, learned_dir=self.learned_dir)
        node.settings = LocalizationSettings()
        node.mode, node.map_name = "localize", "downstairs"
        node.pose_state, node.clean = "tracking", True
        node.unplaced_since = None
        node.last_scan_time = time.monotonic()
        node.activity, node.handled = None, None
        node.tally_map = node.saved_map = node.saved_map_info = node.tally = node.visit = None
        node.visit_driven_m = node.moved_m = 0.0
        node.forget_asked = node.map_png_stale = False
        self.node = node

    def drive(self, scans=4, metres=0.5):
        """A tick per scan, each seeing the wall 2 m ahead from (1, 1), the
        wheels moving `metres` per tick."""
        scan = types.SimpleNamespace(ranges=[2.0] + [math.inf] * 359, angle_min=0.0, angle_increment=math.radians(1), range_min=0.05, range_max=12.0)
        for _ in range(scans):
            self.node.moved_m = metres
            self.node.update_learner()
            self.node.learn(scan, {"x": 1.01, "y": 1.01, "yaw": 0.0})

    def stop_lidar(self, minutes_since_last=31):
        """The lidar goes quiet `minutes_since_last` after the last visit closed."""
        self.node.last_scan_time = None
        self.node.tally["last_closed"] = time.time() - minutes_since_last * 60
        self.node.update_learner()

    def test_a_visit_driven_a_metre_closes_at_the_lidar_stop_and_is_saved(self):
        self.drive()
        self.stop_lidar()
        self.assertIsNone(self.node.visit)
        self.assertEqual(self.node.tally["visits"], 1)
        # the wall's square has its first full visit of the robot's own
        self.assertEqual(self.node.tally["own_full"][20 * 80 + 60], 1)
        saved = map_learner.load_tally(os.path.join(self.learned_dir, "downstairs.npz"))
        self.assertEqual(saved["visits"], 1)

    def test_a_visit_driven_less_than_a_metre_is_dropped(self):
        self.drive(metres=0.1)
        self.stop_lidar()
        self.assertIsNone(self.node.visit)
        self.assertEqual(self.node.tally["visits"], 0)
        self.assertFalse(os.path.exists(self.learned_dir))

    def test_a_lidar_stop_within_half_an_hour_of_the_last_visit_keeps_it_open(self):
        self.drive()
        self.stop_lidar(minutes_since_last=20)
        self.assertIsNotNone(self.node.visit)
        self.assertEqual(self.node.tally["visits"], 0)

    def test_nothing_is_learned_while_unsure_or_held(self):
        for state, clean, handled in (("checking", True, None), ("tracking", False, None), ("tracking", True, "picked up")):
            self.node.pose_state, self.node.clean, self.node.handled = state, clean, handled
            self.drive()
            self.assertIsNone(self.node.visit, state)

    def test_a_map_switch_drops_the_open_visit(self):
        self.drive()
        self.node.map_name = "upstairs"
        self.node.update_learner()
        self.assertIsNone(self.node.visit)
        self.assertIsNone(self.node.tally)

    def test_forgetting_starts_the_tally_over(self):
        self.drive()
        self.stop_lidar()
        self.node.forget_asked = True
        self.node.update_learner()
        self.assertEqual(self.node.tally["visits"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.learned_dir, "downstairs.npz")))

    def test_with_the_learner_off_there_is_no_tally(self):
        self.node.settings = LocalizationSettings(learner="off")
        self.drive()
        self.assertIsNone(self.node.tally)
        self.assertEqual(self.node.handle_request({"cmd": "forget_learned"}), {"ok": False, "error": "not_learning"})


if __name__ == "__main__":
    unittest.main()
