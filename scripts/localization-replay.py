#!/usr/bin/env python3
"""
Replay the nav bridge's recordings through the judge, offline.

Each recording (the bridge's `record` command, or Record on the dashboard
with ?debug) is 10 s of lidar scans with the poses the bridge had. This prints,
per recording, the judge's agree and through shares, and with --truth (a
taped cross: where the robot really stood) the judge at the true pose and
AMCL's error there. With --search it also runs the search on every 4th scan
and prints how often it found a clear winner and, with a truth, how often
that winner was right. Then the beams that always hit the
robot's own body, and where through beams land at true poses: a cluster is a
map error to fix or re-map, spread everywhere means the tolerance is wrong.

The judge's settings come from localization.json (missing means the
defaults), so a tweak can be tried here before the floor.

Usage, on the Pi:
    cd ~/robot-pet
    source .venv/bin/activate
    python scripts/localization-replay.py
    python scripts/localization-replay.py --truth "cross 1=2.10,3.40,90" --audit ~/audit
    python scripts/localization-replay.py --stamp-offset 0.07 --settings /tmp/try.json
    python scripts/localization-replay.py --search --truth "cross 1=2.10,3.40,90"
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import robot_nav
from config.localization import DEFAULT_CONFIG_PATH, load_localization_settings

# a beam this short in nearly every scan is the robot's own body
BODY_RANGE_M = 0.3
BODY_SHARE = 0.9
# through-beam endpoints are counted in squares this big for the audit
AUDIT_CELL_M = 0.25
# --search runs on every this-many scans (about one a second, like the bridge while searching)
SEARCH_EVERY = 4


def parse_truth(text):
    """'cross 1=2.10,3.40,90' -> ('cross 1', {x, y, yaw}); yaw in degrees."""
    label, _, numbers = text.rpartition("=")
    x, y, yaw = (float(value) for value in numbers.split(","))
    return label.strip(), {"x": x, "y": y, "yaw": math.radians(yaw)}


def carried(scans, index, offset):
    """odom -> laser `offset` seconds after scan `index`'s stamp, straight-line
    between it and the neighbouring scan (the bridge recorded it at the stamp)."""
    pose = scans[index]["odom_laser"]
    neighbour = index + 1 if offset > 0 else index - 1
    if offset == 0 or not 0 <= neighbour < len(scans) or scans[neighbour]["odom_laser"] is None:
        return pose
    other = scans[neighbour]["odom_laser"]
    share = abs(offset / (scans[neighbour]["time"] - scans[index]["time"]))
    turned = math.atan2(math.sin(other["yaw"] - pose["yaw"]), math.cos(other["yaw"] - pose["yaw"]))
    return {"x": pose["x"] + share * (other["x"] - pose["x"]), "y": pose["y"] + share * (other["y"] - pose["y"]), "yaw": pose["yaw"] + share * turned}


def degree_runs(degrees):
    """[1, 2, 3, 7] -> '1-3, 7'"""
    runs = []
    for degree in degrees:
        if runs and degree == runs[-1][1] + 1:
            runs[-1][1] = degree
        else:
            runs.append([degree, degree])
    return ", ".join(str(first) if first == last else f"{first}-{last}" for first, last in runs) or "none"


def median(values):
    return f"{np.median(values):.2f}" if values else "--"


def main():
    parser = argparse.ArgumentParser(description="Replay localization recordings through the judge.")
    parser.add_argument("--recordings", default=robot_nav.DEFAULT_RECORDINGS_DIR, help="a folder of .jsonl recordings, or one file")
    parser.add_argument("--maps-dir", default=robot_nav.DEFAULT_MAPS_DIR)
    parser.add_argument("--settings", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--truth", action="append", default=[], help='"label=x,y,yaw_degrees": where the robot really stood (base_link, map frame)')
    parser.add_argument("--stamp-offset", type=float, default=0.0, help="judge the laser pose this many seconds after the scan's stamp")
    parser.add_argument("--audit", help="a folder to draw <map>-through.png into: through beams at true poses")
    parser.add_argument("--search", action="store_true", help="also run the search on the recording's map")
    args = parser.parse_args()

    settings = load_localization_settings(args.settings)
    truths = dict(parse_truth(text) for text in args.truth)
    recordings = Path(args.recordings).expanduser()
    paths = sorted(recordings.glob("*.jsonl")) if recordings.is_dir() else [recordings]
    maps = {}
    # per degree in the laser frame: how often the beam came back shorter than BODY_RANGE_M
    body_hits, body_scans = np.zeros(360), 0
    scan_time = None
    # per map: where through beams ended at true poses, in map metres
    through_ends = {}

    print(f"settings: {json.dumps(settings.to_dict())}")
    print(f"{'recording':40} {'scans':>5} {'agree':>5} {'thru':>5} {'pass':>5}  states       {'true agree':>10} {'thru':>5} {'AMCL off':>13} {'found':>5} {'right':>5}")
    for path in paths:
        lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if not lines:
            continue
        header, scans = lines[0], lines[1:]
        name = header["map"]
        if name not in maps:
            try:
                data, map_info = robot_nav.load_map_file(name, args.maps_dir)
            except (OSError, KeyError, ValueError) as exc:
                print(f"{header['label'][:40]:40} map {name} unreadable: {exc}")
                continue
            maps[name] = data, map_info, robot_nav.search_grids(data, map_info) if args.search else None
        data, map_info, grids = maps[name]
        truth = truths.get(header["label"])
        searched, found, right = 0, 0, 0
        agrees, throughs, passes, true_agrees, true_throughs, amcl_off_m, amcl_off_degrees = [], [], [], [], [], [], []
        states = sorted({scan["state"] for scan in scans if scan["state"]})
        for index, scan in enumerate(scans):
            ranges = [math.inf if value is None else value for value in scan["ranges"]]
            scan_args = (ranges, scan["angle_min"], scan["angle_increment"], scan["range_min"], scan["range_max"])
            measured = robot_nav.degree_ranges(*scan_args)
            body_hits += measured < BODY_RANGE_M
            body_scans += 1
            scan_time = scan["scan_time"]
            if scan["map_odom"] is None or scan["odom_laser"] is None:
                continue
            laser_pose = robot_nav.compose_poses(scan["map_odom"], carried(scans, index, args.stamp_offset))
            check = robot_nav.beam_check(data, map_info, laser_pose, *scan_args, settings)
            winner = None
            if grids is not None and index % SEARCH_EVERY == 0:
                searched += 1
                winner = robot_nav.clear_winner(robot_nav.global_search(grids, *scan_args, settings), settings)
                found += winner is not None
            if check is not None:
                agrees.append(check["agree"])
                throughs.append(check["through"])
                passes.append(check["passes"])
            if truth is None or scan["odom_base"] is None:
                continue
            true_laser = robot_nav.compose_poses(truth, robot_nav.relative_pose(scan["odom_base"], scan["odom_laser"]))
            right += winner is not None and robot_nav.same_place(winner, true_laser)
            true_check = robot_nav.beam_check(data, map_info, true_laser, *scan_args, settings)
            if true_check is not None:
                true_agrees.append(true_check["agree"])
                true_throughs.append(true_check["through"])
                for degree in np.flatnonzero(true_check["classes"] == "through"):
                    angle = true_laser["yaw"] + math.radians(degree)
                    through_ends.setdefault(name, []).append((true_laser["x"] + measured[degree] * math.cos(angle), true_laser["y"] + measured[degree] * math.sin(angle)))
            if scan["amcl"] is not None:
                amcl_off_m.append(math.hypot(scan["amcl"]["x"] - truth["x"], scan["amcl"]["y"] - truth["y"]))
                amcl_off_degrees.append(abs(math.degrees(math.atan2(math.sin(scan["amcl"]["yaw"] - truth["yaw"]), math.cos(scan["amcl"]["yaw"] - truth["yaw"])))))
        pass_share = f"{sum(passes) / len(passes):.2f}" if passes else "--"
        amcl_off = f"{np.median(amcl_off_m) * 100:.0f} cm {np.median(amcl_off_degrees):.0f} deg" if amcl_off_m else "--"
        found_share = f"{found / searched:.2f}" if searched else "--"
        right_share = f"{right / found:.2f}" if found and truth else "--"
        print(f"{header['label'][:40]:40} {len(scans):5} {median(agrees):>5} {median(throughs):>5} {pass_share:>5}  {','.join(states)[:12]:12} {median(true_agrees):>10} {median(true_throughs):>5} {amcl_off:>13} {found_share:>5} {right_share:>5}")

    print()
    if scan_time is not None:
        # how long one turn takes: a --stamp-offset of half this (either sign) judges mid-scan
        print(f"scan time: {scan_time:.3f} s")
    if body_scans:
        print(f"beams shorter than {BODY_RANGE_M} m in {BODY_SHARE:.0%} of {body_scans} scans (the body): {degree_runs(np.flatnonzero(body_hits / body_scans >= BODY_SHARE).tolist())}")
    for name, ends in through_ends.items():
        squares = {}
        for x, y in ends:
            square = (math.floor(x / AUDIT_CELL_M) * AUDIT_CELL_M, math.floor(y / AUDIT_CELL_M) * AUDIT_CELL_M)
            squares[square] = squares.get(square, 0) + 1
        busiest = sorted(squares.items(), key=lambda item: -item[1])[:10]
        print(f"through beams at true poses on {name}: {len(ends)}; busiest {AUDIT_CELL_M} m squares: " + ", ".join(f"({x:.2f}, {y:.2f}) x{count}" for (x, y), count in busiest))
        if args.audit:
            data, map_info, _ = maps[name]
            audit = Path(args.audit).expanduser() / f"{name}-through.png"
            audit.parent.mkdir(parents=True, exist_ok=True)
            robot_nav.render_map_png(map_info["width"], map_info["height"], data, str(audit))
            image = Image.open(audit).convert("RGB")
            for x, y in ends:
                column = int((x - map_info["origin_x"]) / map_info["resolution"])
                row = map_info["height"] - 1 - int((y - map_info["origin_y"]) / map_info["resolution"])
                if 0 <= column < map_info["width"] and 0 <= row < map_info["height"]:
                    image.putpixel((column, row), (255, 0, 0))
            image.save(audit)
            print(f"  drawn in {audit}")


if __name__ == "__main__":
    main()
