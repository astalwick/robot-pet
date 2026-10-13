import importlib.util
import math
import os
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from robot_model import LIDAR_MOUNT, SENSOR_MOUNTS, TRACK_WIDTH_METERS, WHEEL_RADIUS_METERS

spec = importlib.util.spec_from_file_location("generate_urdf", os.path.join(ROOT, "scripts", "generate-urdf.py"))
generate_urdf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate_urdf)


def joint_origins(root):
    """Map child link name -> (xyz tuple, rpy tuple) for every fixed joint."""
    origins = {}
    for joint in root.findall("joint"):
        origin = joint.find("origin")
        origins[joint.find("child").get("link")] = (
            tuple(float(value) for value in origin.get("xyz").split()),
            tuple(float(value) for value in origin.get("rpy").split()),
        )
    return origins


class GenerateUrdfTest(unittest.TestCase):
    def setUp(self):
        self.root = ET.fromstring(generate_urdf.render_urdf())

    def test_every_sensor_mount_has_a_frame_at_its_pose(self):
        links = {link.get("name") for link in self.root.findall("link")}
        origins = joint_origins(self.root)
        for mount in SENSOR_MOUNTS:
            self.assertIn(mount.name, links)
            xyz, rpy = origins[mount.name]
            self.assertEqual(xyz, (round(mount.x, 4), round(mount.y, 4), round(mount.z, 4)))
            self.assertEqual(rpy, (0.0, round(mount.pitch, 4), round(mount.yaw, 4)))

    def test_cliff_sensors_pitch_down(self):
        _, rpy = joint_origins(self.root)["cliff_left"]
        self.assertAlmostEqual(rpy[1], math.radians(35), places=3)

    def test_wheels_sit_at_half_track_on_the_axle(self):
        origins = joint_origins(self.root)
        self.assertEqual(origins["left_wheel"][0], (0.0, round(TRACK_WIDTH_METERS / 2, 4), round(WHEEL_RADIUS_METERS, 4)))
        self.assertEqual(origins["right_wheel"][0], (0.0, -round(TRACK_WIDTH_METERS / 2, 4), round(WHEEL_RADIUS_METERS, 4)))

    def test_laser_frame_sits_at_the_lidar_mount(self):
        # rplidar_ros publishes /scan with frame_id=laser (robot-lidar.service).
        xyz, rpy = joint_origins(self.root)["laser"]
        self.assertEqual(xyz, (round(LIDAR_MOUNT.x, 4), round(LIDAR_MOUNT.y, 4), round(LIDAR_MOUNT.z, 4)))
        self.assertEqual(rpy[2], round(LIDAR_MOUNT.yaw, 4))

    def test_imu_frame_matches_the_imu_message_frame_id(self):
        self.assertIn("imu_link", joint_origins(self.root))

    def test_urdf_survives_the_systemd_parameter_handoff(self):
        # robot-state-publisher.service passes the file as `-p robot_description:=...`,
        # which rcl parses as YAML. A ": " or " #" in the text turns it into a mapping
        # or a comment and the node exits before it starts.
        urdf = generate_urdf.render_urdf()
        self.assertNotIn(": ", urdf)
        self.assertNotIn(" #", urdf)

    def test_checked_in_urdf_is_up_to_date(self):
        with open(os.path.join(ROOT, "urdf", "robot.urdf")) as handle:
            self.assertEqual(handle.read(), generate_urdf.render_urdf(), "run scripts/generate-urdf.py")


if __name__ == "__main__":
    unittest.main()
