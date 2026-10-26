"""Nav bridge logic: saved maps, places, nav.json, the map PNG and the nav socket.

ros_nodes/nav_node.py wires this to ROS (the /map grid, TF, AMCL). Everything
here is plain Python so it runs in the tests without rclpy.

Files, all under ~/.config/robot-pet:

    nav.json      {"mode": "mapping" | "localize", "map": "<name>"}
    places.json   {"<place>": {"map": "<name>", "x": .., "y": .., "yaw": ..}}
    barriers.json {"<map>": [{"x1": .., "y1": .., "x2": .., "y2": ..}, ...]}
    maps/<name>.yaml + .pgm, written by nav2's map_saver_cli
    keepout/<name>.yaml + .pgm, the barriers drawn as a Nav2 keepout mask

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

from robot_model import FOOTPRINT_FRONT_X_METERS, FOOTPRINT_HALF_WIDTH_METERS, FOOTPRINT_REAR_X_METERS
from telemetry.socket_client import send_voice_command

DEFAULT_NAV_CONFIG_PATH = "/home/pi/.config/robot-pet/nav.json"
DEFAULT_PLACES_PATH = "/home/pi/.config/robot-pet/places.json"
DEFAULT_MAPS_DIR = "/home/pi/.config/robot-pet/maps"
DEFAULT_BARRIERS_PATH = "/home/pi/.config/robot-pet/barriers.json"
DEFAULT_KEEPOUT_DIR = "/home/pi/.config/robot-pet/keepout"

# A barrier line is forbidden this far to each side. Keepout cells are not
# inflated, and the tail sweeps 0.31 m from the axle when the robot turns.
BARRIER_HALF_WIDTH_METERS = 0.3
KEEPOUT_RESOLUTION_METERS = 0.05

# AMCL has found the robot when the pose covariance is this tight. 0.25 m² is
# half a meter of standard deviation; a lost filter sits at several meters.
LOCALIZED_XY_VARIANCE = 0.25
LOCALIZED_YAW_VARIANCE = 0.2

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


def is_localized(covariance: list[float] | None) -> bool:
    """Whether a PoseWithCovariance's 6x6 covariance says AMCL has converged."""
    if covariance is None or len(covariance) != 36:
        return False
    return covariance[0] < LOCALIZED_XY_VARIANCE and covariance[7] < LOCALIZED_XY_VARIANCE and covariance[35] < LOCALIZED_YAW_VARIANCE


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
