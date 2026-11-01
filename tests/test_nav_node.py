import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

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


if __name__ == "__main__":
    unittest.main()
