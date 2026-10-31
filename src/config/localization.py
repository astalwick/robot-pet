"""Localization settings: every bar and switch the nav bridge's judge, search
and handling checks use.

The nav bridge reads ~/.config/robot-pet/localization.json at start, so a
change applies on a bridge restart. A missing key means the default. The
replay tool (scripts/localization-replay.py) reads the same file, so a tweak can
be tried on recordings before the floor.

The bars (agree_min, through_max, the tolerance) were read off the ground
floor recordings of 2026-10-30; the table is in
docs/plans/2026-10-26 - localization-plan.md.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/home/pi/.config/robot-pet/localization.json"


class LocalizationSettingsError(ValueError):
    """Raised when a localization settings file exists but cannot be used."""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class LocalizationSettings:
    # A beam agrees when it lands within tolerance_m + tolerance_share x range
    # of where the map says its wall is.
    tolerance_m: float = 0.2
    tolerance_share: float = 0.05
    # each beam is also compared against its neighbours' walls this many degrees each side
    neighbour_rule: bool = True
    neighbour_degrees: int = 3
    # a beam only counts as through a wall when that wall is this close
    through_max_range_m: float = 6.0
    # the largest run of through beams (an open door, a moved sofa) is forgiven
    forgiven_patch: bool = True
    # A pose passes a scan when agree >= agree_min and through <= through_max,
    # and is dropped after fail_scans failed scans in a row.
    # Every true pose recorded agreed 0.55 or more; wrong ones 0.43 or less,
    # but for one by the piano at 0.58. The living room's chair and table
    # legs let up to 26% of beams through at the true pose by the table.
    agree_min: float = 0.55
    through_max: float = 0.25
    fail_scans: int = 4
    # wheel travel since the pose was found before it counts as confirmed
    motion_gate_m: float = 0.3
    # The search commits to a pose that agrees at least agree_min +
    # commit_margin and beats every other place by commit_margin; the gap to
    # agree_min stops the robot flapping between found and lost.
    commit_margin: float = 0.1
    # A place that agrees better than the held pose by rival_margin, named by
    # two searches in a row, drops the held pose (a slip or a half turn).
    # On the floor the true place led a slipped pose by 0.62 or more; while
    # driving correctly nothing led by more than 0.34 (AMCL's heading lagging
    # 10-15 degrees for a few seconds).
    rival: bool = True
    rival_margin: float = 0.4
    # search every saved map, not only the one in use (another floor)
    other_maps: bool = True
    # seconds between searches while the pose is held; back to back while searching
    search_interval_s: float = 5.0
    # The self-check: a scan that fails the map but matches the last scan that
    # passed (carried by the wheels) to self_check_tolerance_m + 2% of range
    # on self_check_min of its beams does not count as failed, so a moved
    # table does not drop a pose the wheels vouch for. A reverse into such a
    # spot matched 0.88 or more; a hidden 20 cm slide or 10 degree turn 0.74
    # or less, and a carry about 0.1.
    self_check: bool = True
    self_check_tolerance_m: float = 0.08
    self_check_min: float = 0.75
    # Picked up while not driving: both cliff sensors see no floor (past
    # their cliff trip in sensors.json). While driving they stay cliff
    # sensors, and with lift_tip on a robot tipped lift_tip_degrees from how
    # it sat counts as picked up instead. Off by default: a pick-up tipped it
    # 11, gentle driving 2.7, but a quick gamepad stop read past 6.
    lift_cliffs: bool = True
    lift_tip: bool = False
    lift_tip_degrees: float = 6.0
    # Pushed while not driving: the robot rocked nudge_degrees within a
    # second (still on the floor 0.17 at most, pushed 1 to 2).
    nudge: bool = True
    nudge_degrees: float = 0.5
    # put down: the floor under both cliff sensors and no rocking for this long
    set_down_s: float = 1.0

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "LocalizationSettings":
        defaults = cls()
        return cls(
            tolerance_m=clamp(float(values.get("tolerance_m", defaults.tolerance_m)), 0.05, 1.0),
            tolerance_share=clamp(float(values.get("tolerance_share", defaults.tolerance_share)), 0.0, 0.2),
            neighbour_rule=bool(values.get("neighbour_rule", defaults.neighbour_rule)),
            neighbour_degrees=int(clamp(float(values.get("neighbour_degrees", defaults.neighbour_degrees)), 1, 10)),
            through_max_range_m=clamp(float(values.get("through_max_range_m", defaults.through_max_range_m)), 1.0, 12.0),
            forgiven_patch=bool(values.get("forgiven_patch", defaults.forgiven_patch)),
            agree_min=clamp(float(values.get("agree_min", defaults.agree_min)), 0.0, 1.0),
            through_max=clamp(float(values.get("through_max", defaults.through_max)), 0.0, 1.0),
            fail_scans=int(clamp(float(values.get("fail_scans", defaults.fail_scans)), 1, 20)),
            motion_gate_m=clamp(float(values.get("motion_gate_m", defaults.motion_gate_m)), 0.0, 3.0),
            commit_margin=clamp(float(values.get("commit_margin", defaults.commit_margin)), 0.0, 0.5),
            rival=bool(values.get("rival", defaults.rival)),
            rival_margin=clamp(float(values.get("rival_margin", defaults.rival_margin)), 0.0, 0.5),
            other_maps=bool(values.get("other_maps", defaults.other_maps)),
            search_interval_s=clamp(float(values.get("search_interval_s", defaults.search_interval_s)), 1.0, 60.0),
            self_check=bool(values.get("self_check", defaults.self_check)),
            self_check_tolerance_m=clamp(float(values.get("self_check_tolerance_m", defaults.self_check_tolerance_m)), 0.02, 0.5),
            self_check_min=clamp(float(values.get("self_check_min", defaults.self_check_min)), 0.0, 1.0),
            lift_cliffs=bool(values.get("lift_cliffs", defaults.lift_cliffs)),
            lift_tip=bool(values.get("lift_tip", defaults.lift_tip)),
            lift_tip_degrees=clamp(float(values.get("lift_tip_degrees", defaults.lift_tip_degrees)), 1.0, 45.0),
            nudge=bool(values.get("nudge", defaults.nudge)),
            nudge_degrees=clamp(float(values.get("nudge_degrees", defaults.nudge_degrees)), 0.1, 10.0),
            set_down_s=clamp(float(values.get("set_down_s", defaults.set_down_s)), 0.5, 10.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


LOCALIZATION_FIELDS = (
    {"key": "tolerance_m", "label": "Beam tolerance (m)", "type": "number", "help": "0.05 .. 1.0; a beam this close to its wall agrees", "min": 0.05, "max": 1.0, "step": 0.01},
    {"key": "tolerance_share", "label": "Tolerance per metre", "type": "number", "help": "0 .. 0.2; added per metre of range", "min": 0.0, "max": 0.2, "step": 0.01},
    {"key": "neighbour_rule", "label": "Compare with neighbour beams", "type": "boolean", "help": "Also accept a neighbour beam's wall"},
    {"key": "neighbour_degrees", "label": "Neighbour degrees", "type": "number", "help": "1 .. 10 each side", "min": 1, "max": 10, "step": 1},
    {"key": "through_max_range_m", "label": "Through-wall range (m)", "type": "number", "help": "1 .. 12; farther walls never count as passed through", "min": 1.0, "max": 12.0, "step": 0.5},
    {"key": "forgiven_patch", "label": "Forgive one patch", "type": "boolean", "help": "Ignore the largest run of through-wall beams (open door, moved sofa)"},
    {"key": "agree_min", "label": "Agree at least", "type": "number", "help": "0 .. 1; share of beams that must fit", "min": 0.0, "max": 1.0, "step": 0.01},
    {"key": "through_max", "label": "Through at most", "type": "number", "help": "0 .. 1; share of beams allowed through walls", "min": 0.0, "max": 1.0, "step": 0.01},
    {"key": "fail_scans", "label": "Failed scans to drop", "type": "number", "help": "1 .. 20 in a row, two a second", "min": 1, "max": 20, "step": 1},
    {"key": "motion_gate_m", "label": "Drive to confirm (m)", "type": "number", "help": "0 .. 3; wheel travel before a found pose counts as sure", "min": 0.0, "max": 3.0, "step": 0.05},
    {"key": "commit_margin", "label": "Search margin", "type": "number", "help": "0 .. 0.5; a found place must agree this much above the bar and above every other place", "min": 0.0, "max": 0.5, "step": 0.01},
    {"key": "rival", "label": "Catch slips", "type": "boolean", "help": "Drop the pose when the search twice finds a clearly better place"},
    {"key": "rival_margin", "label": "Slip margin", "type": "number", "help": "0 .. 0.5; how much better that place must agree", "min": 0.0, "max": 0.5, "step": 0.01},
    {"key": "other_maps", "label": "Search other maps", "type": "boolean", "help": "Also look on the other saved maps (another floor)"},
    {"key": "search_interval_s", "label": "Search every (s)", "type": "number", "help": "1 .. 60, while the robot knows where it is", "min": 1.0, "max": 60.0, "step": 1},
    {"key": "self_check", "label": "Self-check", "type": "boolean", "help": "Keep a pose the map disagrees with when the scan matches the last good one, carried by the wheels"},
    {"key": "self_check_tolerance_m", "label": "Self-check tolerance (m)", "type": "number", "help": "0.02 .. 0.5; a beam this close to the last good scan's matches", "min": 0.02, "max": 0.5, "step": 0.01},
    {"key": "self_check_min", "label": "Self-check match at least", "type": "number", "help": "0 .. 1; share of beams that must match", "min": 0.0, "max": 1.0, "step": 0.01},
    {"key": "lift_cliffs", "label": "Notice pick-ups (cliff sensors)", "type": "boolean", "help": "While not driving, both cliff sensors seeing no floor means picked up"},
    {"key": "lift_tip", "label": "Notice pick-ups while driving", "type": "boolean", "help": "While driving, a tip from how the robot sat means picked up"},
    {"key": "lift_tip_degrees", "label": "Pick-up tip (degrees)", "type": "number", "help": "1 .. 45", "min": 1.0, "max": 45.0, "step": 0.5},
    {"key": "nudge", "label": "Notice pushes", "type": "boolean", "help": "While not driving, the robot rocking means pushed"},
    {"key": "nudge_degrees", "label": "Push rocking (degrees)", "type": "number", "help": "0.1 .. 10 within a second", "min": 0.1, "max": 10.0, "step": 0.1},
    {"key": "set_down_s", "label": "Put down after (s)", "type": "number", "help": "0.5 .. 10 of floor under both cliff sensors and no rocking", "min": 0.5, "max": 10.0, "step": 0.5},
)


def load_localization_settings(path: str = DEFAULT_CONFIG_PATH) -> LocalizationSettings:
    config_path = Path(path)
    try:
        values = json.loads(config_path.read_text())
    except FileNotFoundError:
        return LocalizationSettings()
    except json.JSONDecodeError as exc:
        raise LocalizationSettingsError(f"Invalid localization settings at {config_path}: {exc}") from exc

    if not isinstance(values, dict):
        raise LocalizationSettingsError(f"Invalid localization settings at {config_path}: expected a JSON object")

    try:
        return LocalizationSettings.from_dict(values)
    except (TypeError, ValueError) as exc:
        raise LocalizationSettingsError(f"Invalid localization settings at {config_path}: {exc}") from exc


def save_localization_settings(settings: LocalizationSettings, path: str = DEFAULT_CONFIG_PATH):
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(settings.to_dict(), indent=2, sort_keys=True) + "\n"

    with tempfile.NamedTemporaryFile(
        "w",
        dir=config_path.parent,
        prefix=f".{config_path.name}.",
        delete=False,
    ) as file_obj:
        file_obj.write(data)
        temp_name = file_obj.name

    os.replace(temp_name, config_path)
