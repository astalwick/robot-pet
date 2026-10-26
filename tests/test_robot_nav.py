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

    def test_barriers_round_trip_and_default_to_empty(self):
        path = os.path.join(self.tmpdir.name, "barriers.json")
        self.assertEqual(robot_nav.load_barriers(path), {})
        barriers = {"main": [{"x1": 0.0, "y1": 1.0, "x2": 2.0, "y2": 1.0}]}
        robot_nav.save_barriers(barriers, path)
        self.assertEqual(robot_nav.load_barriers(path), barriers)

    def test_valid_barrier_needs_four_numbers(self):
        self.assertTrue(robot_nav.valid_barrier({"cmd": "create_barrier", "x1": 0, "y1": 1.5, "x2": 2, "y2": 1.5}))
        self.assertFalse(robot_nav.valid_barrier({"x1": 0, "y1": 1.5, "x2": "2", "y2": 1.5}))
        self.assertFalse(robot_nav.valid_barrier({"x1": 0, "y1": 1.5}))
        self.assertFalse(robot_nav.valid_barrier(None))

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


class ScanFitTest(unittest.TestCase):
    # A 5 m square room at 5 cm: the outer ring of cells is wall, the rest free.
    ROOM = {"width": 100, "height": 100, "resolution": 0.05, "origin_x": 0.0, "origin_y": 0.0}

    def room(self):
        grid = np.zeros((100, 100), dtype=np.int16)
        grid[0, :] = grid[-1, :] = grid[:, 0] = grid[:, -1] = 100
        return grid.ravel().tolist()

    def ray_cast(self, x, y, yaw, beams=360):
        """Ranges a lidar at (x, y, yaw) sees in the room: the inner wall faces
        are at 0.05 and 4.95 m."""
        ranges = []
        for beam in range(beams):
            angle = yaw + math.radians(beam)
            hits = []
            if math.cos(angle) > 1e-9:
                hits.append((4.95 - x) / math.cos(angle))
            elif math.cos(angle) < -1e-9:
                hits.append((0.05 - x) / math.cos(angle))
            if math.sin(angle) > 1e-9:
                hits.append((4.95 - y) / math.sin(angle))
            elif math.sin(angle) < -1e-9:
                hits.append((0.05 - y) / math.sin(angle))
            ranges.append(min(hits))
        return ranges

    def fit(self, ranges, laser_pose):
        return robot_nav.scan_fit(self.room(), self.ROOM, laser_pose, ranges, 0.0, math.radians(1), 0.15, 12.0)

    def test_the_scan_fits_from_the_true_pose(self):
        ranges = self.ray_cast(2.0, 1.5, 0.3)
        self.assertGreater(self.fit(ranges, {"x": 2.0, "y": 1.5, "yaw": 0.3}), 0.95)

    def test_the_scan_does_not_fit_from_the_wrong_pose(self):
        ranges = self.ray_cast(2.0, 1.5, 0.0)
        # believed to face 45 degrees off: the room's outline lands across the floor and off the map
        self.assertLess(self.fit(ranges, {"x": 2.0, "y": 1.5, "yaw": math.radians(45)}), robot_nav.SCAN_FIT_LOST)
        # a metre off: only the beams along the walls still match
        self.assertLess(self.fit(ranges, {"x": 3.0, "y": 1.5, "yaw": 0.0}), 0.6)

    def test_invalid_beams_do_not_count_and_too_few_is_no_opinion(self):
        ranges = self.ray_cast(2.5, 2.5, 0.0)
        ranges[:180] = [math.inf] * 90 + [0.1] * 90  # out of range and under range_min
        self.assertGreater(self.fit(ranges, {"x": 2.5, "y": 2.5, "yaw": 0.0}), 0.95)
        ranges[180:340] = [math.nan] * 160
        self.assertIsNone(self.fit(ranges, {"x": 2.5, "y": 2.5, "yaw": 0.0}))

    def test_localized_change_rule(self):
        lost_run, found_run = robot_nav.SCAN_FIT_LOST_TICKS, robot_nav.SCAN_FIT_FOUND_TICKS
        # while localized: only a long enough run of bad fits loses it
        self.assertIsNone(robot_nav.localized_change(True, 0, 0))
        self.assertIsNone(robot_nav.localized_change(True, lost_run - 1, 0))
        self.assertIsNone(robot_nav.localized_change(True, 0, found_run))
        self.assertEqual(robot_nav.localized_change(True, lost_run, 0), (False, "the scan does not match the map here"))
        # while lost: only a run of good fits finds it
        self.assertIsNone(robot_nav.localized_change(False, 0, 0))
        self.assertIsNone(robot_nav.localized_change(False, 0, found_run - 1))
        self.assertIsNone(robot_nav.localized_change(False, lost_run, 0))
        self.assertEqual(robot_nav.localized_change(False, 0, found_run), (True, "the scan fits the map here"))


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


class RenderKeepoutMaskTest(unittest.TestCase):
    def read_mask(self, barriers):
        with tempfile.TemporaryDirectory() as tmpdir:
            stem = os.path.join(tmpdir, "keepout", "main")
            robot_nav.render_keepout_mask(barriers, stem)
            self.assertEqual(sorted(os.listdir(os.path.dirname(stem))), ["main.pgm", "main.yaml"])
            with open(f"{stem}.yaml") as file_obj:
                yaml_text = file_obj.read()
            return np.asarray(Image.open(f"{stem}.pgm")), yaml_text

    def test_a_barrier_is_a_black_band_on_a_white_grid_the_barriers_size(self):
        # A 1 m line along x at y = 0: the band reaches 0.3 m to each side, the
        # grid 0.35 m beyond the line's ends (a cell of white around the band).
        pixels, yaml_text = self.read_mask([{"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 0.0}])
        self.assertIn("image: main.pgm\n", yaml_text)
        self.assertIn("resolution: 0.05\n", yaml_text)
        self.assertIn("origin: [-0.350, -0.350, 0]\n", yaml_text)
        self.assertEqual(pixels.shape, (14, 34))  # 0.7 m tall, 1.7 m wide

        def pixel_at(x, y):  # map metres to the image, row 0 at the top
            column = int((x + 0.35) / 0.05)
            row = pixels.shape[0] - 1 - int((y + 0.35) / 0.05)
            return pixels[row, column]

        self.assertEqual(pixel_at(0.5, 0.0), 0)  # on the line
        self.assertEqual(pixel_at(0.5, 0.27), 0)  # inside the band
        self.assertEqual(pixel_at(0.5, 0.33), 255)  # outside it
        self.assertEqual(pixel_at(1.27, 0.0), 0)  # the band runs past the end
        self.assertEqual(pixel_at(1.33, 0.0), 255)
        self.assertEqual(pixels[0, 0], 255)  # corners are free

    def test_no_barriers_is_a_small_free_grid(self):
        pixels, yaml_text = self.read_mask([])
        self.assertTrue((pixels == 255).all())
        self.assertIn("origin: [-0.350, -0.350, 0]\n", yaml_text)

    def test_distance_to_segment(self):
        barrier = {"x1": 0.0, "y1": 0.0, "x2": 2.0, "y2": 0.0}
        self.assertAlmostEqual(float(robot_nav.distance_to_segment(1.0, 0.5, barrier)), 0.5)
        self.assertAlmostEqual(float(robot_nav.distance_to_segment(3.0, 0.0, barrier)), 1.0)  # past the end
        self.assertAlmostEqual(float(robot_nav.distance_to_segment(1.0, 1.0, {"x1": 1.0, "y1": 0.0, "x2": 1.0, "y2": 0.0})), 1.0)  # a point


class BarrierClearancesTest(unittest.TestCase):
    """Body edges: 0.064 m ahead of the axle, 0.266 m behind, 0.1655 m to each side."""

    FACING_X = {"x": 0.0, "y": 0.0, "yaw": 0.0}
    ACROSS_AT_X1 = {"x1": 1.0, "y1": -2.0, "x2": 1.0, "y2": 2.0}

    def test_a_barrier_ahead_is_measured_from_the_front_edge(self):
        forward_m, rear_m = robot_nav.barrier_clearances(self.FACING_X, [self.ACROSS_AT_X1])
        self.assertAlmostEqual(forward_m, 1.0 - 0.064)
        self.assertEqual(rear_m, math.inf)

    def test_a_barrier_behind_is_measured_from_the_rear_edge(self):
        behind = {"x1": -1.0, "y1": -2.0, "x2": -1.0, "y2": 2.0}
        forward_m, rear_m = robot_nav.barrier_clearances(self.FACING_X, [behind])
        self.assertEqual(forward_m, math.inf)
        self.assertAlmostEqual(rear_m, 1.0 - 0.266)

    def test_the_pose_and_heading_count(self):
        # facing -y from (3, 5): the line at y = 4 is 1 m ahead of the axle
        pose = {"x": 3.0, "y": 5.0, "yaw": -math.pi / 2}
        forward_m, _ = robot_nav.barrier_clearances(pose, [{"x1": 0.0, "y1": 4.0, "x2": 6.0, "y2": 4.0}])
        self.assertAlmostEqual(forward_m, 1.0 - 0.064)

    def test_a_barrier_beside_the_body_is_not_in_the_way(self):
        beside = {"x1": -2.0, "y1": 0.5, "x2": 2.0, "y2": 0.5}
        self.assertEqual(robot_nav.barrier_clearances(self.FACING_X, [beside]), (math.inf, math.inf))

    def test_a_short_barrier_counts_only_where_it_crosses_the_body_width(self):
        # a line from (2, 0.1) back to (1, 1.1): it is inside the body's width
        # only up to y = 0.1655, which is x = 1.9345, so that is the nearest point
        forward_m, _ = robot_nav.barrier_clearances(self.FACING_X, [{"x1": 2.0, "y1": 0.1, "x2": 1.0, "y2": 1.1}])
        self.assertAlmostEqual(forward_m, 1.9345 - 0.064)
        # the same line starting past the body's width is not in the way
        self.assertEqual(robot_nav.barrier_clearances(self.FACING_X, [{"x1": 1.0, "y1": 0.2, "x2": 2.0, "y2": 1.2}]), (math.inf, math.inf))

    def test_a_barrier_through_the_front_edge_leaves_no_room_ahead_only(self):
        through = {"x1": -0.1, "y1": 0.0, "x2": 0.5, "y2": 0.0}
        forward_m, rear_m = robot_nav.barrier_clearances(self.FACING_X, [through])
        self.assertEqual(forward_m, 0.0)
        self.assertEqual(rear_m, math.inf)

    def test_the_nearest_of_several_barriers_wins(self):
        further = {"x1": 2.0, "y1": -2.0, "x2": 2.0, "y2": 2.0}
        forward_m, _ = robot_nav.barrier_clearances(self.FACING_X, [further, self.ACROSS_AT_X1])
        self.assertAlmostEqual(forward_m, 1.0 - 0.064)
        self.assertEqual(robot_nav.barrier_clearances(self.FACING_X, []), (math.inf, math.inf))


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
