import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

import numpy as np

import map_learner
import robot_nav
from config.localization import LocalizationSettings

SETTINGS = LocalizationSettings()
# a 4 m x 2 m room at 5 cm, its origin at (0, 0)
MAP_INFO = {"width": 80, "height": 40, "resolution": 0.05, "origin_x": 0.0, "origin_y": 0.0}


def row_of(squares):
    """The map_info of a map one square high."""
    return {"width": squares, "height": 1, "resolution": 0.05, "origin_x": 0.0, "origin_y": 0.0}


def visits(tally, mark, count, square=0):
    """Close `count` visits that saw one square as full or clear."""
    for _ in range(count):
        visit = map_learner.new_visit(tally["marks"].shape[1])
        visit["scans"][square] = 2
        visit["looks"][square] = 10
        visit["hits"][square] = 10 if mark == map_learner.FULL else 0
        map_learner.close_visit(tally, visit, row_of(tally["marks"].shape[1]), SETTINGS)


def strict(tally, square=0):
    return int(map_learner.strict_map(tally)[square])


def planning(tally, square=0):
    return int(map_learner.planning_map(tally, SETTINGS)[square])


def one_beam_scan(range_m):
    """A scan whose only return is straight ahead, at range_m (None: no return)."""
    ranges = [math.inf] * 360
    ranges[0] = math.inf if range_m is None else range_m
    return ranges, 0.0, math.radians(1), 0.05, 12.0


class TallyTest(unittest.TestCase):
    def test_with_nothing_learned_both_maps_are_the_saved_map(self):
        saved = np.array([100, 0, -1], dtype=np.int8)
        tally = map_learner.new_tally(saved)
        np.testing.assert_array_equal(map_learner.strict_map(tally), saved)
        np.testing.assert_array_equal(map_learner.planning_map(tally, SETTINGS), saved)

    def test_a_removed_coffee_table_goes_on_its_third_clear_visit(self):
        tally = map_learner.new_tally([100])
        visits(tally, map_learner.CLEAR, 2)
        self.assertEqual(strict(tally), 100)
        visits(tally, map_learner.CLEAR, 1)
        self.assertEqual(strict(tally), 0)
        # three full of six visits, at half a wall's cost
        self.assertEqual(planning(tally), 25)

    def test_a_table_seen_on_all_ten_visits_takes_five_and_is_grey_for_routes_meanwhile(self):
        tally = map_learner.new_tally([100])
        visits(tally, map_learner.FULL, 10)
        visits(tally, map_learner.CLEAR, 2)
        self.assertEqual(strict(tally), 100)
        self.assertEqual(planning(tally), 40)
        visits(tally, map_learner.CLEAR, 2)
        self.assertEqual(strict(tally), 100)
        visits(tally, map_learner.CLEAR, 1)
        self.assertEqual(strict(tally), 0)

    def test_a_new_couch_on_white_floor_turns_black_on_its_fourth_full_visit(self):
        tally = map_learner.new_tally([0])
        visits(tally, map_learner.FULL, 3)
        self.assertEqual(strict(tally), 0)
        visits(tally, map_learner.FULL, 1)
        self.assertEqual(strict(tally), 100)
        # it never changed back, so it is a wall for routes too
        self.assertEqual(planning(tally), 100)

    def test_a_door_seen_open_and_shut_stays_grey_for_routes_even_shut_for_ten_visits(self):
        tally = map_learner.new_tally([0])
        visits(tally, map_learner.FULL, 2)
        visits(tally, map_learner.CLEAR, 2)
        visits(tally, map_learner.FULL, 10)
        self.assertEqual(strict(tally), 100)
        self.assertEqual(planning(tally), 50)

    def test_two_bad_clear_visits_leave_a_wall_seen_ten_times_black(self):
        tally = map_learner.new_tally([100])
        visits(tally, map_learner.FULL, 10)
        visits(tally, map_learner.CLEAR, 2)
        self.assertEqual(strict(tally), 100)

    def test_unknown_stays_unknown_until_seen_on_two_visits(self):
        tally = map_learner.new_tally([-1])
        visits(tally, map_learner.CLEAR, 1)
        self.assertEqual(strict(tally), -1)
        self.assertEqual(planning(tally), 0)
        visits(tally, map_learner.CLEAR, 1)
        self.assertEqual(strict(tally), 0)

    def close(self, scans, looks, hits):
        """One visit over a row of squares; the newest mark of each."""
        tally = map_learner.new_tally([0] * len(scans))
        visit = map_learner.new_visit(len(scans))
        visit["scans"][:], visit["looks"][:], visit["hits"][:] = scans, looks, hits
        map_learner.close_visit(tally, visit, row_of(len(scans)), SETTINGS)
        # the saved map's three clear visits are under the newest
        return [mark if count == 4 else map_learner.NONE for mark, count in zip(tally["marks"][-1].tolist(), (tally["marks"] >= 0).sum(axis=0))]

    def test_a_square_one_scan_looked_at_gets_no_mark(self):
        marks = self.close(scans=[1, 0, 0, 0, 0, 2], looks=[5, 0, 0, 0, 0, 5], hits=[5, 0, 0, 0, 0, 5])
        self.assertEqual(marks[0], map_learner.NONE)
        self.assertEqual(marks[5], map_learner.FULL)

    def test_a_square_hit_by_a_third_of_its_looks_is_full(self):
        marks = self.close(scans=[2, 0, 0, 0, 0, 2], looks=[9, 0, 0, 0, 0, 9], hits=[3, 0, 0, 0, 0, 2])
        self.assertEqual(marks[0], map_learner.FULL)
        self.assertEqual(marks[5], map_learner.CLEAR)

    def test_a_square_beams_ended_within_10_cm_of_is_not_seen_through(self):
        # a wall seen 5 cm off: its square 2 is crossed, the beams end in square 3
        marks = self.close(scans=[2] * 6, looks=[9] * 6, hits=[0, 0, 0, 9, 0, 0])
        self.assertEqual(marks, [map_learner.CLEAR, map_learner.NONE, map_learner.NONE, map_learner.FULL, map_learner.NONE, map_learner.NONE])


class LearnScanTest(unittest.TestCase):
    def learn(self, range_m, settings=SETTINGS):
        visit = map_learner.new_visit(MAP_INFO["width"] * MAP_INFO["height"])
        # the lidar just inside the first square of row 20, facing +x
        map_learner.learn_scan(visit, MAP_INFO, {"x": 0.01, "y": 1.01, "yaw": 0.0}, *one_beam_scan(range_m), settings)
        return visit

    def test_a_beam_hits_its_end_square_and_looks_at_each_square_on_the_way_once(self):
        visit = self.learn(1.0)
        row = visit["looks"].reshape(40, 80)[20]
        hits = visit["hits"].reshape(40, 80)[20]
        # the end square (x 1.01 m) is hit, and it and the squares before it looked at once each
        self.assertEqual(np.flatnonzero(hits).tolist(), [20])
        self.assertEqual(np.flatnonzero(row).tolist(), list(range(21)))
        self.assertEqual(row.max(), 1)
        self.assertEqual(visit["looks"].sum(), 21)

    def test_a_beam_with_no_return_teaches_nothing(self):
        self.assertEqual(self.learn(None)["looks"].sum(), 0)

    def test_a_long_beam_looks_only_within_learn_range(self):
        visit = self.learn(3.5, LocalizationSettings(learn_range_m=2.0))
        self.assertEqual(visit["hits"].sum(), 0)
        self.assertEqual(np.flatnonzero(visit["looks"].reshape(40, 80)[20]).tolist(), list(range(40)))


class ChangesTest(unittest.TestCase):
    def test_new_gone_and_coming_and_going_squares_each_get_their_colour(self):
        saved = [0, 100, 0, 0]
        tally = map_learner.new_tally(saved)
        visits(tally, map_learner.FULL, 4, square=0)
        visits(tally, map_learner.CLEAR, 3, square=1)
        visits(tally, map_learner.FULL, 2, square=2)
        visits(tally, map_learner.CLEAR, 2, square=2)
        changes = map_learner.changes(tally, saved)
        self.assertEqual(changes[map_learner.ADDED_COLOUR].tolist(), [True, False, False, False])
        self.assertEqual(changes[map_learner.REMOVED_COLOUR].tolist(), [False, True, False, False])
        self.assertEqual(changes[map_learner.CHANGED_COLOUR].tolist(), [False, False, True, False])

    def test_one_visit_against_the_map_is_pale_and_can_be_left_off(self):
        saved = [0, 100, 0, 100]
        tally = map_learner.new_tally(saved)
        visits(tally, map_learner.FULL, 1, square=0)
        visits(tally, map_learner.CLEAR, 1, square=1)
        visits(tally, map_learner.CLEAR, 1, square=2)
        visits(tally, map_learner.FULL, 1, square=3)
        changes = map_learner.changes(tally, saved)
        self.assertEqual(changes[map_learner.EARLY_ADDED_COLOUR].tolist(), [True, False, False, False])
        self.assertEqual(changes[map_learner.EARLY_REMOVED_COLOUR].tolist(), [False, True, False, False])
        self.assertFalse(changes[map_learner.ADDED_COLOUR].any() or changes[map_learner.REMOVED_COLOUR].any())
        # drawn first, so the full colours paint over them
        self.assertEqual(list(changes)[:2], [map_learner.EARLY_ADDED_COLOUR, map_learner.EARLY_REMOVED_COLOUR])
        self.assertEqual(list(map_learner.changes(tally, saved, early=False)), [map_learner.ADDED_COLOUR, map_learner.REMOVED_COLOUR, map_learner.CHANGED_COLOUR])


class TallyFileTest(unittest.TestCase):
    def test_a_saved_tally_loads_back_as_saved(self):
        tally = map_learner.new_tally([100, 0, -1])
        visits(tally, map_learner.FULL, 2, square=1)
        tally["visits"], tally["last_closed"] = 2, 1761998400.5
        with tempfile.TemporaryDirectory() as learned_dir:
            path = os.path.join(learned_dir, "learned", "downstairs.npz")
            map_learner.save_tally(tally, path)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["downstairs.npz"])
            loaded = map_learner.load_tally(path)
        for key in ("marks", "own_full", "own_clear"):
            np.testing.assert_array_equal(loaded[key], tally[key])
        self.assertEqual((loaded["visits"], loaded["last_closed"]), (2, 1761998400.5))


class MapFileTest(unittest.TestCase):
    def test_a_written_map_reads_back_as_written(self):
        data = np.array([100, 0, -1, 0, 100, -1], dtype=np.int8)
        map_info = {"width": 3, "height": 2, "resolution": 0.05, "origin_x": -1.5, "origin_y": 2.25}
        with tempfile.TemporaryDirectory() as maps_dir:
            map_learner.write_map_file(data, map_info, os.path.join(maps_dir, "downstairs"))
            loaded, loaded_info = robot_nav.load_map_file("downstairs", maps_dir)
        np.testing.assert_array_equal(loaded, data)
        self.assertEqual(loaded_info, map_info)


if __name__ == "__main__":
    unittest.main()
