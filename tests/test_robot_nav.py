import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np
from PIL import Image

import robot_nav
from robot_nav import NavSocketServer, request_nav


class NavConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.nav_path = os.path.join(self.tmpdir.name, "nav.json")
        self.places_path = os.path.join(self.tmpdir.name, "places.json")
        self.maps_dir = os.path.join(self.tmpdir.name, "maps")

    def save_map_files(self, name):
        os.makedirs(self.maps_dir, exist_ok=True)
        open(robot_nav.map_yaml_path(name, self.maps_dir), "w").close()

    def test_missing_or_broken_nav_json_means_mapping(self):
        self.assertEqual(robot_nav.load_nav_config(self.nav_path), {"mode": "mapping", "map": None})
        with open(self.nav_path, "w") as file_obj:
            file_obj.write("{not json")
        self.assertEqual(robot_nav.load_nav_config(self.nav_path), {"mode": "mapping", "map": None})
        with open(self.nav_path, "w") as file_obj:
            json.dump({"mode": "dance", "map": "main"}, file_obj)
        self.assertEqual(robot_nav.load_nav_config(self.nav_path), {"mode": "mapping", "map": None})

    def test_nav_json_round_trips(self):
        robot_nav.save_nav_config("localize", "main", self.nav_path)
        self.assertEqual(robot_nav.load_nav_config(self.nav_path), {"mode": "localize", "map": "main"})
        self.assertEqual(os.listdir(self.tmpdir.name), ["nav.json"])  # no temp file left behind

    def test_places_round_trip_and_default_to_empty(self):
        self.assertEqual(robot_nav.load_places(self.places_path), {})
        places = {"kitchen": {"map": "main", "x": 1.0, "y": 2.0, "yaw": 0.5}}
        robot_nav.save_places(places, self.places_path)
        self.assertEqual(robot_nav.load_places(self.places_path), places)

    def test_saved_maps_lists_yaml_stems_sorted(self):
        self.assertEqual(robot_nav.saved_maps(self.maps_dir), [])  # directory does not exist yet
        self.save_map_files("upstairs")
        self.save_map_files("main")
        open(os.path.join(self.maps_dir, "main.pgm"), "w").close()
        self.assertEqual(robot_nav.saved_maps(self.maps_dir), ["main", "upstairs"])

    def test_resolve_mode_falls_back_to_mapping_when_the_map_is_gone(self):
        nav = {"mode": "localize", "map": "main"}
        self.assertEqual(robot_nav.resolve_mode(nav, self.maps_dir), ("mapping", None))
        self.save_map_files("main")
        self.assertEqual(robot_nav.resolve_mode(nav, self.maps_dir), ("localize", "main"))
        self.assertEqual(robot_nav.resolve_mode({"mode": "mapping", "map": None}, self.maps_dir), ("mapping", None))

    def test_maps_to_try_puts_the_last_used_map_first(self):
        self.assertEqual(robot_nav.maps_to_try(["main", "upstairs"], "upstairs"), ["upstairs", "main"])
        self.assertEqual(robot_nav.maps_to_try(["main", "upstairs"], None), ["main", "upstairs"])
        self.assertEqual(robot_nav.maps_to_try(["main", "upstairs"], "deleted"), ["main", "upstairs"])


class NavMathTest(unittest.TestCase):
    def covariance(self, x, y, yaw):
        values = [0.0] * 36
        values[0], values[7], values[35] = x, y, yaw
        return values

    def test_is_localized_needs_tight_xy_and_yaw(self):
        self.assertTrue(robot_nav.is_localized(self.covariance(0.1, 0.1, 0.05)))
        self.assertFalse(robot_nav.is_localized(self.covariance(1.0, 0.1, 0.05)))
        self.assertFalse(robot_nav.is_localized(self.covariance(0.1, 1.0, 0.05)))
        self.assertFalse(robot_nav.is_localized(self.covariance(0.1, 0.1, 0.5)))
        self.assertFalse(robot_nav.is_localized(None))
        self.assertFalse(robot_nav.is_localized([0.0] * 9))

    def test_yaw_from_quaternion(self):
        self.assertAlmostEqual(robot_nav.yaw_from_quaternion(0, 0, 0, 1), 0.0)
        # 90 degrees about z
        self.assertAlmostEqual(robot_nav.yaw_from_quaternion(0, 0, 0.7071068, 0.7071068), 1.5707963, places=5)
        self.assertAlmostEqual(robot_nav.yaw_from_quaternion(0, 0, 1, 0), 3.1415927, places=5)

    def test_yaw_toward_faces_the_target(self):
        self.assertAlmostEqual(robot_nav.yaw_toward(0, 0, 1, 0), 0.0)
        self.assertAlmostEqual(robot_nav.yaw_toward(0, 0, 0, 2), math.pi / 2)
        self.assertAlmostEqual(robot_nav.yaw_toward(1, 1, 0, 1), math.pi)

    def test_nearest_place_only_looks_at_this_map(self):
        places = {
            "kitchen": {"map": "main", "x": 1.0, "y": 0.0, "yaw": 0.0},
            "living room": {"map": "main", "x": 4.0, "y": 0.0, "yaw": 0.0},
            "bedroom": {"map": "upstairs", "x": 0.1, "y": 0.0, "yaw": 0.0},
        }
        name, distance = robot_nav.nearest_place(places, "main", 0.0, 0.0)
        self.assertEqual(name, "kitchen")
        self.assertAlmostEqual(distance, 1.0)
        self.assertEqual(robot_nav.nearest_place(places, "upstairs", 5.0, 0.0), ("bedroom", 4.9))
        self.assertIsNone(robot_nav.nearest_place(places, None, 0.0, 0.0))
        self.assertEqual(robot_nav.places_on_map(places, "main"), ["kitchen", "living room"])

    def test_valid_names(self):
        self.assertTrue(robot_nav.valid_map_name("main"))
        self.assertTrue(robot_nav.valid_map_name("floor-2_b"))
        self.assertFalse(robot_nav.valid_map_name("Main"))
        self.assertFalse(robot_nav.valid_map_name("../etc"))
        self.assertFalse(robot_nav.valid_map_name("living room"))
        self.assertFalse(robot_nav.valid_map_name(""))
        self.assertFalse(robot_nav.valid_map_name(None))
        self.assertTrue(robot_nav.valid_place_name("living room"))
        self.assertFalse(robot_nav.valid_place_name("kitchen!"))


class RenderMapPngTest(unittest.TestCase):
    def test_png_is_flipped_with_free_white_occupied_black_unknown_gray(self):
        # 3 wide, 2 tall. Grid row 0 is the bottom of the map.
        data = [0, 100, -1, 50, 0, 0]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "map.png")
            robot_nav.render_map_png(3, 2, data, path)
            pixels = np.asarray(Image.open(path))
            self.assertEqual(os.listdir(tmpdir), ["map.png"])

        self.assertEqual(pixels.shape, (2, 3))
        self.assertEqual(list(pixels[1]), [255, 0, 128])  # grid row 0 at the bottom
        self.assertEqual(list(pixels[0]), [128, 255, 255])  # 50% occupied is mid gray


class NavSocketServerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.socket_path = os.path.join(self.tmpdir.name, "nav.sock")
        self.requests = []
        self.server = NavSocketServer(self.socket_path, self.handle)
        self.server.start()
        self.addCleanup(self.server.stop)

    def handle(self, request):
        self.requests.append(request)
        return {"ok": True, "cmd": request["cmd"]}

    def test_round_trip(self):
        reply = request_nav(self.socket_path, "save_map", name="main")

        self.assertEqual(reply, {"ok": True, "cmd": "save_map"})
        self.assertEqual(self.requests, [{"cmd": "save_map", "name": "main"}])

    def test_bad_request_is_answered_not_dropped(self):
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(self.socket_path)
            conn.sendall(b"not json\n")
            reply = conn.makefile("rb").readline()

        self.assertEqual(json.loads(reply), {"ok": False, "error": "bad_request"})
        self.assertEqual(self.requests, [])

    def test_unreachable_socket_returns_an_error_dict(self):
        reply = request_nav(os.path.join(self.tmpdir.name, "nope.sock"), "nav_status")

        self.assertEqual(reply, {"ok": False, "error": "nav_socket_unavailable"})


if __name__ == "__main__":
    unittest.main()
