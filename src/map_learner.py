"""The learner: the map follows the house.

Each square of a map keeps its last ten visits, each one full (the robot saw
something there) or clear (it saw through it). The saved map counts as three
visits to start. Two maps are made from that tally:

    the strict map    black, white or unknown, for AMCL, the judge and the
                      search, read as they read the saved map
    the planning map  for Nav2's routes: walls black, everything else seen a
                      grey for how often it was full, so a door shut on most
                      visits costs but never forbids

Each square also counts the robot's own full and clear visits, up to two
each. Once both reach two the square has changed (a door, a chair), and it
is never a wall on the planning map again.

A tally is a dict of flat arrays, one entry per OccupancyGrid cell (row 0 at
the bottom, like robot_nav.load_map_file):

    marks      VISITS_KEPT x cells: FULL, CLEAR or NONE, oldest first
    own_full   the robot's own full visits, up to CHANGED_VISITS
    own_clear  its own clear visits, up to CHANGED_VISITS

and two numbers: visits (the robot's own visits closed) and last_closed
(when the last one closed, in seconds since the epoch, 0 before the first).
The bridge keeps it in learned/<map>.npz (save_tally, load_tally).

An open visit is a dict of flat counts, added to scan by scan: looks (beams
that looked at a square), hits (beams that ended in it) and scans (scans
that looked at it at all).

The rules are in docs/plans/2026-10-26 - localization-plan.md, section 7.
Everything here is plain Python and NumPy, so it runs in the tests and in
scripts/localization-replay.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

import robot_nav
from config.localization import LocalizationSettings

# These shape the tally from its first visit, so changing them means forgetting.
VISITS_KEPT = 10
SEED_VISITS = 3
CHANGED_VISITS = 2

FULL, CLEAR, NONE = 1, 0, -1

# the dashboard's colours for what the learner changed (changes)
ADDED_COLOUR, REMOVED_COLOUR, CHANGED_COLOUR = (220, 50, 50), (40, 170, 90), (240, 150, 20)


def new_tally(data) -> dict[str, np.ndarray]:
    """A tally that starts from a saved map: black squares with three full
    visits, white with three clear, unknown with none."""
    data = np.asarray(data)
    marks = np.full((VISITS_KEPT, data.size), NONE, dtype=np.int8)
    marks[-SEED_VISITS:, data >= robot_nav.OCCUPIED_THRESHOLD] = FULL
    marks[-SEED_VISITS:, data == 0] = CLEAR
    return {"marks": marks, "own_full": np.zeros(data.size, dtype=np.int8), "own_clear": np.zeros(data.size, dtype=np.int8), "visits": 0, "last_closed": 0.0}


def save_tally(tally: dict[str, Any], path: str) -> None:
    """Write the tally to an .npz, by way of a temporary file so a power cut
    never leaves half of one."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(f"{path}.tmp", "wb") as file:
        np.savez(file, **tally)
    os.replace(f"{path}.tmp", path)


def load_tally(path: str) -> dict[str, Any]:
    with np.load(path) as saved:
        return {
            "marks": saved["marks"],
            "own_full": saved["own_full"],
            "own_clear": saved["own_clear"],
            "visits": int(saved["visits"]),
            "last_closed": float(saved["last_closed"]),
        }


def new_visit(cells: int) -> dict[str, np.ndarray]:
    return {"looks": np.zeros(cells, dtype=np.int32), "hits": np.zeros(cells, dtype=np.int32), "scans": np.zeros(cells, dtype=np.int32)}


def learn_scan(
    visit: dict[str, np.ndarray],
    map_info: dict[str, Any],
    laser_pose: dict[str, float],
    ranges,
    angle_min: float,
    angle_increment: float,
    range_min: float,
    range_max: float,
    settings: LocalizationSettings,
) -> None:
    """Add what one scan says to the open visit. For each degree's beam that
    came back, within learn_range_m: the square it ended in was looked at and
    hit; the squares it crossed on the way were looked at. A beam with no
    return teaches nothing, and one longer than learn_range_m only looks at
    the squares within it."""
    resolution = map_info["resolution"]
    measured = robot_nav.degree_ranges(ranges, angle_min, angle_increment, range_min, range_max)
    returned = np.flatnonzero(np.isfinite(measured))
    measured = measured[returned]
    angles = laser_pose["yaw"] + np.radians(returned)
    cos, sin = np.cos(angles), np.sin(angles)

    within = measured <= settings.learn_range_m
    ends = np.where(within, square_at(map_info, laser_pose["x"] + cos * measured, laser_pose["y"] + sin * measured), -1)

    # the squares each beam crossed before its end square, in half-cell steps, each counted once per beam
    steps = np.arange(0, settings.learn_range_m, resolution / 2)
    crossing = steps < np.minimum(measured, settings.learn_range_m)[:, None]
    beams = np.broadcast_to(np.arange(len(returned))[:, None], crossing.shape)[crossing]
    crossed = square_at(map_info, laser_pose["x"] + (cos[:, None] * steps)[crossing], laser_pose["y"] + (sin[:, None] * steps)[crossing])
    keep = (crossed >= 0) & (crossed != ends[beams])
    crossed = np.unique(beams[keep] * visit["looks"].size + crossed[keep]) % visit["looks"].size
    ends = ends[ends >= 0]

    looks = np.concatenate([crossed, ends])
    visit["looks"] += np.bincount(looks, minlength=visit["looks"].size)
    visit["hits"] += np.bincount(ends, minlength=visit["hits"].size)
    visit["scans"][np.unique(looks)] += 1


def square_at(map_info: dict[str, Any], x, y):
    """The flat cell index of each map point, -1 off the map."""
    columns = np.floor((x - map_info["origin_x"]) / map_info["resolution"]).astype(int)
    rows = np.floor((y - map_info["origin_y"]) / map_info["resolution"]).astype(int)
    on_map = (columns >= 0) & (columns < map_info["width"]) & (rows >= 0) & (rows < map_info["height"])
    return np.where(on_map, rows * map_info["width"] + columns, -1)


def close_visit(tally: dict[str, np.ndarray], visit: dict[str, np.ndarray], map_info: dict[str, Any], settings: LocalizationSettings) -> None:
    """Give every square the visit looked at from two scans or more its mark
    for the visit, onto the end of its last ten: full when at least
    learn_full_share of its looks ended in it (a wall seen at a slant has
    beams grazing through it), clear when fewer than that ended within
    learn_near_m of it, and no mark in between: a wall the lidar sees a few
    cm off from the map is not seen through. Squares it did not look at keep
    their visits as they were."""
    reach = round(settings.learn_near_m / map_info["resolution"])
    hits = np.pad(visit["hits"].reshape(map_info["height"], map_info["width"]), reach)
    near_hits = np.lib.stride_tricks.sliding_window_view(hits, (2 * reach + 1, 2 * reach + 1)).sum(axis=(2, 3)).ravel()
    enough = settings.learn_full_share * visit["looks"]
    full = visit["hits"] >= enough
    looked = (visit["scans"] >= 2) & (full | (near_hits < enough))
    full = full[looked]
    marks = tally["marks"]
    marks[:, looked] = np.roll(marks[:, looked], -1, axis=0)
    marks[-1, looked] = np.where(full, FULL, CLEAR)
    tally["own_full"][looked] = np.minimum(tally["own_full"][looked] + full, CHANGED_VISITS)
    tally["own_clear"][looked] = np.minimum(tally["own_clear"][looked] + ~full, CHANGED_VISITS)


def strict_map(tally: dict[str, np.ndarray]) -> np.ndarray:
    """OccupancyGrid data for AMCL, the judge and the search: black when a
    square's visits are more often full than clear and at least three are
    full, white when it is not black and was seen on two visits, unknown
    otherwise. A door open half the time is white, so the judge still sees
    the wall behind it."""
    full, clear = (tally["marks"] == FULL).sum(axis=0), (tally["marks"] == CLEAR).sum(axis=0)
    black = (full > clear) & (full >= 3)
    return np.select([black, full + clear >= 2], [100, 0], -1).astype(np.int8)


def planning_map(tally: dict[str, np.ndarray], settings: LocalizationSettings) -> np.ndarray:
    """OccupancyGrid data for Nav2's routes: unknown where never seen, a wall
    where the strict map is black and the square never changed, and every
    other square seen a grey of planning_grey times its share of full visits."""
    full, clear = (tally["marks"] == FULL).sum(axis=0), (tally["marks"] == CLEAR).sum(axis=0)
    changed = (tally["own_full"] >= CHANGED_VISITS) & (tally["own_clear"] >= CHANGED_VISITS)
    with np.errstate(invalid="ignore"):
        grey = np.round(100 * settings.planning_grey * full / (full + clear))
    return np.select([full + clear == 0, (strict_map(tally) == 100) & ~changed], [-1, 100], grey).astype(np.int8)


def changes(tally: dict[str, Any], saved) -> dict[tuple[int, int, int], np.ndarray]:
    """What the dashboard draws over the map, {colour: squares}: red where the
    strict map is black and the saved map white, green where it is white and
    the saved map black, and orange where a square is marked changed (drawn
    last, over the other two)."""
    strict, saved = strict_map(tally), np.asarray(saved)
    return {
        ADDED_COLOUR: (strict == 100) & (saved == 0),
        REMOVED_COLOUR: (strict == 0) & (saved >= robot_nav.OCCUPIED_THRESHOLD),
        CHANGED_COLOUR: (tally["own_full"] >= CHANGED_VISITS) & (tally["own_clear"] >= CHANGED_VISITS),
    }


def write_map_file(data, map_info: dict[str, Any], stem: str) -> None:
    """Write <stem>.pgm + <stem>.yaml the way map_saver does (trinary:
    black 0, white 254, unknown 205), so map_server, AMCL and
    robot_nav.load_map_file read it like a saved map. Each file goes to a
    temporary name first and is renamed over the old one, so a power cut
    never leaves half a map."""
    grid = np.asarray(data).reshape(map_info["height"], map_info["width"])
    pixels = np.select([grid >= robot_nav.OCCUPIED_THRESHOLD, grid == 0], [0, 254], 205).astype(np.uint8)
    path = Path(stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.flipud(pixels), "L").save(f"{stem}.pgm.tmp", format="PPM")
    os.replace(f"{stem}.pgm.tmp", f"{stem}.pgm")
    Path(f"{stem}.yaml.tmp").write_text(
        f"image: {path.name}.pgm\nmode: trinary\nresolution: {map_info['resolution']:.3f}\n"
        f"origin: [{map_info['origin_x']:.3f}, {map_info['origin_y']:.3f}, 0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
    )
    os.replace(f"{stem}.yaml.tmp", f"{stem}.yaml")
