"""Single source of truth for the robot's physical geometry.

Pure constants and small frozen dataclasses — no hardware, no framework imports.
This survives the ROS2 migration unchanged and feeds the eventual URDF/TF tree,
the differential-drive odometry (control/odometry.py), and the velocity
kinematics (control/differential_drive.py).

Coordinate frame: REP-103 `base_link`. Origin is the drive-axle midpoint at
ground level, on the centerline. +x is forward, +y is left, +z is up. Angles are
radians, distances meters. Positive yaw is counterclockwise; positive pitch tilts
a sensor's forward/measurement axis downward (rotation about +y in a z-up frame).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# --- Drive train ---------------------------------------------------------

# 5203-2402-0019 motors (537.7 counts per output-shaft revolution) with nominal
# 96 mm Hogback wheels mounted directly on the output shaft. Calibrated
# 2026-10-25 on tile: three 2.0 m encoder moves measured 2.105, 2.101 and
# 2.102 m, so the effective diameter is 4.35 % over nominal. This scales every
# distance move and odometry.
WHEEL_DIAMETER_METERS = 0.1002
WHEEL_RADIUS_METERS = WHEEL_DIAMETER_METERS / 2
ENCODER_COUNTS_PER_WHEEL_REVOLUTION = 537.7
ENCODER_COUNTS_PER_METER = ENCODER_COUNTS_PER_WHEEL_REVOLUTION / (math.pi * WHEEL_DIAMETER_METERS)

# Effective track width, calibrated 2026-10-25 on tile with
# scripts/diagnostics/motion-turn.py --timed: four open-loop three-revolution
# turns, two each way, all implied 0.3200 m from the wheel-distance difference
# against the IMU. (IMU-closed `turn` runs scattered 0.316 to 0.321 because the
# overshoot correction reverses the wheels.) The wheel centres measure
# 0.308 m (2026-10-26); the wide contact patch scrubs in a turn, which is why the effective
# width is larger. Only affects heading in odometry and the twist-to-wheel-speed
# split, not straight-line distance.
TRACK_WIDTH_METERS = 0.320


# --- Body footprint (for the nav costmap; loose tolerance) ---------------

# Measured 2026-10-26 (docs/robot dimensions.md). The bumper front face sits 51 mm
# ahead of the drive axle (the base_link origin). The front edge is the cliff-sensor
# tips, 9 mm past the bumper. The rear edge is the caster wheel swung fully back,
# 335 mm behind the bumper face. The wheels make it 332 mm wide.
BUMPER_FRONT_X_METERS = 0.051
FOOTPRINT_FRONT_X_METERS = 0.060
FOOTPRINT_REAR_X_METERS = -0.284
FOOTPRINT_HALF_WIDTH_METERS = 0.166


# --- Sensor mounts -------------------------------------------------------

@dataclass(frozen=True)
class SensorMount:
    """One sensor's pose in base_link (REP-103), meters and radians.

    `name` matches the sensor name in sensors.json so a future TF publisher can
    join a mount to its live reading. `pitch` is positive for a downward-facing
    sensor (e.g. the cliff ToFs point their beam 35 degrees below horizontal).
    """

    name: str
    x: float
    y: float
    z: float
    yaw: float = 0.0
    pitch: float = 0.0


CLIFF_DOWN_PITCH = math.radians(35)

# Forward VL53L1X ToFs look straight ahead; x is the sensor chip's face (measured
# 2026-10-26). The side sensors stand on posts 24 mm in from the frame sides, 112 mm
# behind the bumper (so they land behind the axle); the center one is 39.5 mm behind.
# Cliff VL53L0X ToFs sit on the bumper ends, angled down toward the floor.
SENSOR_MOUNTS: tuple[SensorMount, ...] = (
    SensorMount("forward_left", x=-0.061, y=0.112, z=0.124),
    SensorMount("forward_center", x=0.0115, y=0.0, z=0.110),
    SensorMount("forward_right", x=-0.061, y=-0.112, z=0.124),
    SensorMount("cliff_left", x=0.060, y=0.128, z=0.067, pitch=CLIFF_DOWN_PITCH),
    SensorMount("cliff_right", x=0.060, y=-0.128, z=0.067, pitch=CLIFF_DOWN_PITCH),
)

# RPLidar A1 on the deck, measured 2026-10-25: rotation centre 82 mm behind the
# axle on the centreline. Scan plane 196 mm above the floor (2026-10-26: top of
# the lidar at 205 mm, laser about 9 mm below it). The motor and cable
# point backwards, so the A1's 0 degree mark faces forward (yaw 0).
LIDAR_MOUNT = SensorMount("laser", x=-0.082, y=0.0, z=0.196)

# BNO085 board (Adafruit 4754, 25.6 x 22.7 mm) standing upright on a post near the
# rear-left of the deck, measured 2026-10-26: centre 88.2 mm forward of the
# platform rear and 24 mm in from the left edge of the 272 mm frame, board top 135.5 mm above the
# floor, so the board centre is about 124 mm up.
IMU_MOUNT = SensorMount("imu_link", x=-0.1278, y=0.112, z=0.124)
