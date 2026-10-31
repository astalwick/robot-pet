"""Nav bridge logic: saved maps, places, nav.json, the map PNG and the nav socket.

ros_nodes/nav_node.py wires this to ROS (the /map grid, TF, AMCL). Everything
here is plain Python so it runs in the tests without rclpy.

Files, all under ~/.config/robot-pet:

    nav.json      {"mode": "mapping" | "localize", "map": "<name>"}
    places.json   {"<place>": {"map": "<name>", "x": .., "y": .., "yaw": ..}}
    barriers.json {"<map>": [{"x1": .., "y1": .., "x2": .., "y2": ..}, ...]}
    maps/<name>.yaml + .pgm, written by nav2's map_saver_cli
    keepout/<name>.yaml + .pgm, the barriers drawn as a Nav2 keepout mask
    localization.json, the judge's bars and switches (config/localization.py)
    recordings/<time>-<label>.jsonl, scans saved for scripts/localization-replay.py

config/nav.launch.py reads nav.json at robot-nav.service start; the bridge
writes it and restarts the unit to switch modes.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from config.localization import LocalizationSettings
from robot_model import FOOTPRINT_FRONT_X_METERS, FOOTPRINT_HALF_WIDTH_METERS, FOOTPRINT_REAR_X_METERS
from telemetry.socket_client import send_voice_command

DEFAULT_NAV_CONFIG_PATH = "/home/pi/.config/robot-pet/nav.json"
DEFAULT_PLACES_PATH = "/home/pi/.config/robot-pet/places.json"
DEFAULT_MAPS_DIR = "/home/pi/.config/robot-pet/maps"
DEFAULT_BARRIERS_PATH = "/home/pi/.config/robot-pet/barriers.json"
DEFAULT_KEEPOUT_DIR = "/home/pi/.config/robot-pet/keepout"
DEFAULT_RECORDINGS_DIR = "/home/pi/.config/robot-pet/recordings"

# A barrier line is forbidden this far to each side. Keepout cells are not
# inflated, and the tail sweeps 0.31 m from the axle when the robot turns.
BARRIER_HALF_WIDTH_METERS = 0.3
KEEPOUT_RESOLUTION_METERS = 0.05

# Is the robot where it thinks it is? scan_fit is the share of lidar beams
# whose end points land within SCAN_FIT_CELLS of a wall the map knows about.
# A robot at its true pose scores high; one carried somewhere else scores
# like random. AMCL's covariance cannot tell: it grades its particles on a
# curve, so a cloud that matches nothing still has a confident best guess.
SCAN_FIT_CELLS = 3
SCAN_FIT_MIN_BEAMS = 30
# map_server marks walls 100, free 0 and unknown -1
OCCUPIED_THRESHOLD = 50
# Floor numbers (ground floor, 2026-10-26): 55-90% at the true pose, 44-49% at
# the right spot facing the wrong way, 23% on the kitchen counter. Until the
# search replaces the relocalize spin, a spin is accepted only at
# SCAN_FIT_FOUND or better and when beam_check passes too.
SCAN_FIT_FOUND = 0.6

# Map names become file names; place names are spoken, so they may have spaces.
MAP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
PLACE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9 _-]{0,31}")


def load_nav_config(path: str = DEFAULT_NAV_CONFIG_PATH) -> dict[str, Any]:
    """{"mode", "map"}; a missing or broken file means a robot that maps."""
    try:
        values = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"mode": "mapping", "map": None}
    if not isinstance(values, dict) or values.get("mode") not in ("mapping", "localize"):
        return {"mode": "mapping", "map": None}
    return {"mode": values["mode"], "map": values.get("map") or None}


def save_nav_config(mode: str, map_name: str | None, path: str = DEFAULT_NAV_CONFIG_PATH) -> None:
    _write_json(path, {"mode": mode, "map": map_name})


def load_places(path: str = DEFAULT_PLACES_PATH) -> dict[str, dict[str, Any]]:
    try:
        values = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def save_places(places: dict[str, dict[str, Any]], path: str = DEFAULT_PLACES_PATH) -> None:
    _write_json(path, places)


def load_barriers(path: str = DEFAULT_BARRIERS_PATH) -> dict[str, list[dict[str, float]]]:
    try:
        values = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def save_barriers(barriers: dict[str, list[dict[str, float]]], path: str = DEFAULT_BARRIERS_PATH) -> None:
    _write_json(path, barriers)


def _write_json(path: str, data: Any) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=config_path.parent, prefix=f".{config_path.name}.", delete=False
    ) as file_obj:
        file_obj.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        temp_name = file_obj.name
    os.replace(temp_name, config_path)


def saved_maps(maps_dir: str = DEFAULT_MAPS_DIR) -> list[str]:
    """Names of the saved maps, sorted."""
    return sorted(path.stem for path in Path(maps_dir).glob("*.yaml"))


def map_yaml_path(name: str, maps_dir: str = DEFAULT_MAPS_DIR) -> str:
    return os.path.join(maps_dir, f"{name}.yaml")


def resolve_mode(nav: dict[str, Any], maps_dir: str = DEFAULT_MAPS_DIR) -> tuple[str, str | None]:
    """What nav.launch.py will actually run for this nav.json: localize needs
    a map that still exists on disk, anything else is mapping."""
    if nav["mode"] == "localize" and nav["map"] and os.path.exists(map_yaml_path(nav["map"], maps_dir)):
        return "localize", nav["map"]
    return "mapping", None


def maps_to_try(maps: list[str], last_used: str | None) -> list[str]:
    """Relocalization order: the map we were on last, then the rest."""
    if last_used in maps:
        return [last_used] + [name for name in maps if name != last_used]
    return list(maps)


def scan_fit(
    data, map_info: dict[str, Any], laser_pose: dict[str, float], ranges, angle_min: float, angle_increment: float, range_min: float, range_max: float
) -> float | None:
    """The share (0..1) of a lidar scan's beams that end within SCAN_FIT_CELLS
    of an occupied map cell, given the lidar's pose in the map frame. None when
    too few beams are valid to say anything. Beams that end off the map miss."""
    ranges = np.asarray(ranges, dtype=np.float64)
    valid = np.isfinite(ranges) & (ranges >= range_min) & (ranges <= range_max)
    if valid.sum() < SCAN_FIT_MIN_BEAMS:
        return None
    angles = laser_pose["yaw"] + angle_min + angle_increment * np.flatnonzero(valid)
    ranges = ranges[valid]
    columns = np.floor((laser_pose["x"] + ranges * np.cos(angles) - map_info["origin_x"]) / map_info["resolution"]).astype(int)
    rows = np.floor((laser_pose["y"] + ranges * np.sin(angles) - map_info["origin_y"]) / map_info["resolution"]).astype(int)
    width, height = map_info["width"], map_info["height"]
    grid = np.asarray(data, dtype=np.int16).reshape(height, width)
    # near_wall: occupied cells grown by SCAN_FIT_CELLS in every direction
    occupied = np.pad(grid >= OCCUPIED_THRESHOLD, SCAN_FIT_CELLS)
    near_wall = np.zeros((height, width), dtype=bool)
    for shift_y in range(2 * SCAN_FIT_CELLS + 1):
        for shift_x in range(2 * SCAN_FIT_CELLS + 1):
            near_wall |= occupied[shift_y : shift_y + height, shift_x : shift_x + width]
    on_map = (columns >= 0) & (columns < width) & (rows >= 0) & (rows < height)
    return float(near_wall[rows[on_map], columns[on_map]].sum() / len(ranges))


def beam_check(
    data,
    map_info: dict[str, Any],
    laser_pose: dict[str, float],
    ranges,
    angle_min: float,
    angle_increment: float,
    range_min: float,
    range_max: float,
    settings: LocalizationSettings,
) -> dict[str, Any] | None:
    """The judge: does this pose contradict the map? One beam per degree is
    ray-cast across the map from the lidar's pose and its measured range is
    sorted into one of:

        agree     within tolerance of its own wall or a neighbour beam's
        through   well beyond the farthest of those walls: it went through one
        short     something unmapped is in the way (a person, a chair)
        none      the ray reaches unknown map before any wall: no opinion
        no_return the lidar got nothing back (black furniture), never through

    The largest run of through beams is forgiven (an open door, a moved sofa):
    it is not counted as through but stays in the agree share's denominator,
    so a wrong pose cannot gain by it. Returns {"agree", "through" (shares of
    the beams with an opinion), "opinion" (share of returned beams with one),
    "passes", "classes" (per degree)}, or None when there is no verdict: fewer
    than a third of the returned beams have an opinion, or too few returned."""
    resolution = map_info["resolution"]
    width, height = map_info["width"], map_info["height"]
    grid = np.asarray(data, dtype=np.int16).reshape(height, width)

    # one measured beam per degree, the scan beam nearest it
    ranges = np.asarray(ranges, dtype=np.float64)
    degrees = np.radians(np.arange(360))
    measured = ranges[np.round((degrees - angle_min) / angle_increment).astype(int) % len(ranges)]
    returned = np.isfinite(measured) & (measured >= range_min) & (measured <= range_max)
    if returned.sum() < SCAN_FIT_MIN_BEAMS:
        return None

    # ray-cast in half-cell steps to the first wall or unknown cell (off the map is unknown)
    angles = laser_pose["yaw"] + degrees
    distances = np.arange(1, int(range_max / (resolution / 2)) + 1) * (resolution / 2)
    columns = np.floor((laser_pose["x"] + np.cos(angles)[:, None] * distances - map_info["origin_x"]) / resolution).astype(int)
    rows = np.floor((laser_pose["y"] + np.sin(angles)[:, None] * distances - map_info["origin_y"]) / resolution).astype(int)
    on_map = (columns >= 0) & (columns < width) & (rows >= 0) & (rows < height)
    cells = np.full(columns.shape, -1, dtype=np.int16)
    cells[on_map] = grid[rows[on_map], columns[on_map]]
    stops = (cells >= OCCUPIED_THRESHOLD) | (cells < 0)
    first_stop = stops.argmax(axis=1)
    reaches_unknown = stops.any(axis=1) & (cells[np.arange(360), first_stop] < 0)
    # the wall each beam should see; inf when it sees unknown or nothing in range
    predicted = np.where(stops.any(axis=1) & ~reaches_unknown, distances[first_stop], np.inf)

    # each beam against its own wall and its neighbours', always in the
    # direction that makes "through" harder to claim
    spread = settings.neighbour_degrees if settings.neighbour_rule else 0
    neighbours = np.stack([np.roll(predicted, shift) for shift in range(-spread, spread + 1)])
    tolerance = settings.tolerance_m + settings.tolerance_share * measured
    agree = returned & np.any(np.abs(neighbours - measured) <= tolerance, axis=0)
    no_opinion = returned & ~agree & reaches_unknown
    farthest = neighbours.max(axis=0)
    through = returned & ~agree & ~no_opinion & (farthest <= settings.through_max_range_m) & (measured > farthest + 2 * tolerance)
    classes = np.full(360, "short", dtype="<U9")
    classes[~returned] = "no_return"
    classes[no_opinion] = "none"
    classes[agree] = "agree"
    classes[through] = "through"

    opinion = np.flatnonzero(returned & ~no_opinion)
    if len(opinion) * 3 < returned.sum():
        return None
    if settings.forgiven_patch and through.any():
        # Go round the scan over only the beams with an opinion, so a missing
        # return in the middle of an open door does not split it in two, and
        # start just after a beam that is not through, so no run wraps the end.
        flags = through[opinion]
        start = int(np.argmin(flags)) + 1
        largest_run: list[int] = []
        run: list[int] = []
        for offset in range(len(flags)):
            index = (start + offset) % len(flags)
            run = run + [int(opinion[index])] if flags[index] else []
            if len(run) > len(largest_run):
                largest_run = run
        classes[largest_run] = "forgiven"
    agree_share = float((classes == "agree").sum() / len(opinion))
    through_share = float((classes == "through").sum() / len(opinion))
    return {
        "agree": agree_share,
        "through": through_share,
        "opinion": float(len(opinion) / returned.sum()),
        "passes": agree_share >= settings.agree_min and through_share <= settings.through_max,
        "classes": classes,
    }


def compose_poses(first: dict[str, float], second: dict[str, float]) -> dict[str, float]:
    """second, given in first's frame, in the frame first is given in
    (map -> odom with odom -> laser is the laser in the map)."""
    cos_yaw, sin_yaw = math.cos(first["yaw"]), math.sin(first["yaw"])
    return {
        "x": first["x"] + cos_yaw * second["x"] - sin_yaw * second["y"],
        "y": first["y"] + sin_yaw * second["x"] + cos_yaw * second["y"],
        "yaw": math.atan2(math.sin(first["yaw"] + second["yaw"]), math.cos(first["yaw"] + second["yaw"])),
    }


def relative_pose(origin: dict[str, float], pose: dict[str, float]) -> dict[str, float]:
    """pose in origin's frame, both given in the same frame: how the robot
    moved from origin to pose, as seen from origin."""
    dx, dy = pose["x"] - origin["x"], pose["y"] - origin["y"]
    cos_yaw, sin_yaw = math.cos(origin["yaw"]), math.sin(origin["yaw"])
    turned = pose["yaw"] - origin["yaw"]
    return {"x": cos_yaw * dx + sin_yaw * dy, "y": cos_yaw * dy - sin_yaw * dx, "yaw": math.atan2(math.sin(turned), math.cos(turned))}


def yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def yaw_toward(from_x: float, from_y: float, to_x: float, to_y: float) -> float:
    """The heading that faces (to_x, to_y) from (from_x, from_y)."""
    return math.atan2(to_y - from_y, to_x - from_x)


def places_on_map(places: dict[str, dict[str, Any]], map_name: str | None) -> list[str]:
    return sorted(name for name, place in places.items() if place.get("map") == map_name)


def nearest_place(
    places: dict[str, dict[str, Any]], map_name: str | None, x: float, y: float
) -> tuple[str, float] | None:
    """(name, distance in m) of the closest saved place on this map, or None."""
    candidates = [
        (math.hypot(place["x"] - x, place["y"] - y), name)
        for name, place in places.items()
        if place.get("map") == map_name
    ]
    if not candidates:
        return None
    distance, name = min(candidates)
    return name, distance


def render_map_png(width: int, height: int, data, path: str) -> None:
    """Write an OccupancyGrid as a grayscale PNG: free white, occupied black,
    unknown gray. Grid row 0 is the bottom of the map, image row 0 the top."""
    grid = np.asarray(data, dtype=np.int16).reshape(height, width)
    pixels = np.where(grid < 0, 128, 255 - grid * 255 // 100).astype(np.uint8)
    temp_path = f"{path}.tmp"
    Image.fromarray(np.flipud(pixels), "L").save(temp_path, format="PNG")
    os.replace(temp_path, path)


def render_keepout_mask(barriers: list[dict[str, float]], stem: str) -> None:
    """Write <stem>.pgm + <stem>.yaml, the Nav2 keepout mask for one map: a
    white grid at 5 cm, just big enough for the barriers, with each one drawn
    as a black band. Nav2 reads the mask in the map frame through the yaml's
    origin and resolution, so it needs no relation to the map's own grid."""
    margin = BARRIER_HALF_WIDTH_METERS + KEEPOUT_RESOLUTION_METERS
    xs = [barrier[key] for barrier in barriers for key in ("x1", "x2")] or [0.0]
    ys = [barrier[key] for barrier in barriers for key in ("y1", "y2")] or [0.0]
    origin_x, origin_y = min(xs) - margin, min(ys) - margin
    width = math.ceil((max(xs) + margin - origin_x) / KEEPOUT_RESOLUTION_METERS)
    height = math.ceil((max(ys) + margin - origin_y) / KEEPOUT_RESOLUTION_METERS)
    # cell centres, grid row 0 at the bottom like an OccupancyGrid
    cell_x, cell_y = np.meshgrid(
        origin_x + (np.arange(width) + 0.5) * KEEPOUT_RESOLUTION_METERS,
        origin_y + (np.arange(height) + 0.5) * KEEPOUT_RESOLUTION_METERS,
    )
    blocked = np.zeros((height, width), dtype=bool)
    for barrier in barriers:
        blocked |= distance_to_segment(cell_x, cell_y, barrier) <= BARRIER_HALF_WIDTH_METERS
    pixels = np.where(blocked, 0, 255).astype(np.uint8)
    Path(stem).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.flipud(pixels), "L").save(f"{stem}.pgm")
    Path(f"{stem}.yaml").write_text(
        f"image: {Path(stem).name}.pgm\nmode: trinary\nresolution: {KEEPOUT_RESOLUTION_METERS}\n"
        f"origin: [{origin_x:.3f}, {origin_y:.3f}, 0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
    )


def distance_to_segment(x, y, barrier: dict[str, float]):
    """Distance from (x, y), scalars or arrays, to the barrier's line segment."""
    dx, dy = barrier["x2"] - barrier["x1"], barrier["y2"] - barrier["y1"]
    length_sq = dx * dx + dy * dy
    along = 0.0 if length_sq == 0 else np.clip(((x - barrier["x1"]) * dx + (y - barrier["y1"]) * dy) / length_sq, 0.0, 1.0)
    return np.hypot(x - (barrier["x1"] + along * dx), y - (barrier["y1"] + along * dy))


def barrier_clearances(pose: dict[str, float], barriers: list[dict[str, float]]) -> tuple[float, float]:
    """How far the body's front and rear edges are from the nearest barrier
    straight ahead and straight behind, in metres (inf: none in the way).

    The barrier twin of safety_gate.scan_clearances, so robot-motion can stop
    short of a line the way it stops short of a lidar return. Each segment
    goes into the robot frame and is clipped to the body's width; a segment
    that cuts through the footprint counts on whichever ends it pokes out of.
    """
    cos_yaw, sin_yaw = math.cos(pose["yaw"]), math.sin(pose["yaw"])
    forward_m = rear_m = math.inf
    for barrier in barriers:
        dx1, dy1 = barrier["x1"] - pose["x"], barrier["y1"] - pose["y"]
        dx2, dy2 = barrier["x2"] - pose["x"], barrier["y2"] - pose["y"]
        ax, ay = dx1 * cos_yaw + dy1 * sin_yaw, dy1 * cos_yaw - dx1 * sin_yaw
        bx, by = dx2 * cos_yaw + dy2 * sin_yaw, dy2 * cos_yaw - dx2 * sin_yaw
        # the part of the segment (as a fraction 0..1 of it) inside the body's width
        if ay == by:
            if abs(ay) > FOOTPRINT_HALF_WIDTH_METERS:
                continue
            start, end = 0.0, 1.0
        else:
            start, end = sorted(((-FOOTPRINT_HALF_WIDTH_METERS - ay) / (by - ay), (FOOTPRINT_HALF_WIDTH_METERS - ay) / (by - ay)))
            start, end = max(start, 0.0), min(end, 1.0)
            if start > end:
                continue
        nearest_x, farthest_x = sorted((ax + start * (bx - ax), ax + end * (bx - ax)))
        if farthest_x > FOOTPRINT_FRONT_X_METERS:
            forward_m = min(forward_m, max(nearest_x, FOOTPRINT_FRONT_X_METERS) - FOOTPRINT_FRONT_X_METERS)
        if nearest_x < FOOTPRINT_REAR_X_METERS:
            rear_m = min(rear_m, FOOTPRINT_REAR_X_METERS - min(farthest_x, FOOTPRINT_REAR_X_METERS))
    return forward_m, rear_m


def valid_barrier(barrier: Any) -> bool:
    return isinstance(barrier, dict) and all(isinstance(barrier.get(key), (int, float)) for key in ("x1", "y1", "x2", "y2"))


def valid_map_name(name: Any) -> bool:
    return isinstance(name, str) and MAP_NAME_RE.fullmatch(name) is not None


def valid_place_name(name: Any) -> bool:
    return isinstance(name, str) and PLACE_NAME_RE.fullmatch(name) is not None


def request_nav(socket_path: str, cmd: str, **parameters: Any) -> dict[str, Any]:
    """Send one command to the nav socket and return its reply, or an error
    dict if the bridge is not reachable (never raises)."""
    reply = send_voice_command(socket_path, {"cmd": cmd, **parameters})
    if reply is None:
        return {"ok": False, "error": "nav_socket_unavailable"}
    return reply


class NavSocketServer:
    """Unix-socket server thread: one JSON line in, one JSON line out, per
    connection. `handle(request) -> reply` runs on the connection's own thread
    so a slow command does not hold up the next one."""

    _ACCEPT_TIMEOUT = 0.5

    def __init__(self, socket_path: str, handle: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self.socket_path = socket_path
        self.handle = handle
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o660)
        server.listen(4)
        server.settimeout(self._ACCEPT_TIMEOUT)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="nav-socket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(target=self._serve_connection, args=(conn,), name="nav-socket-conn", daemon=True).start()

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(2.0)
            try:
                line = conn.makefile("rb").readline(4096)
                request = json.loads(line.decode("utf-8"))
            except (OSError, ValueError):
                request = None
            if not isinstance(request, dict):
                reply = {"ok": False, "error": "bad_request"}
            else:
                reply = self.handle(request)
            try:
                conn.sendall((json.dumps(reply, separators=(",", ":")) + "\n").encode("utf-8"))
            except OSError:
                pass
