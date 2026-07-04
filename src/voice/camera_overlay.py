"""Navigation overlays on camera JPEGs sent to the voice model."""

from __future__ import annotations

import math

HALF_FOV_H_DEGREES = 51.0
ROBOT_HALF_WIDTH_M = 0.166
CORRIDOR_DISTANCES_M = (0.5, 1.0)

RULER_TICK_DEGREES = range(-50, 51, 10)
RULER_LABELS = {20: "L20", 40: "L40", -20: "R20", -40: "R40"}

CROSSHAIR_COLOR = (255, 255, 255)
RULER_COLOR = (120, 255, 120)
CORRIDOR_COLORS = ((0, 220, 255), (0, 255, 255))
LINE_THICKNESS = 1
FONT = 0
FONT_SCALE = 0.45
FONT_THICKNESS = 1


def angle_to_x(theta_degrees: float, image_width: int) -> int:
    """Map an angle left (+) or right (-) of center to a pixel column."""
    center = image_width / 2
    half_span = image_width / 2
    x = center - (theta_degrees / HALF_FOV_H_DEGREES) * half_span
    return int(round(x))


def _corridor_half_angle(distance_m: float) -> float:
    return math.degrees(math.atan(ROBOT_HALF_WIDTH_M / distance_m))


def annotate_snapshot(jpeg_bytes: bytes) -> bytes:
    """Draw the navigation overlay on a camera JPEG and return new JPEG bytes."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return jpeg_bytes

    image = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jpeg_bytes

    height, width = image.shape[:2]
    overlay = image.copy()
    center_x = angle_to_x(0.0, width)

    cv2.line(overlay, (center_x, 0), (center_x, height - 1), CROSSHAIR_COLOR, LINE_THICKNESS)

    ruler_y = height - 12
    tick_top = ruler_y - 6
    tick_bottom = ruler_y + 6
    for degrees in RULER_TICK_DEGREES:
        x = angle_to_x(float(degrees), width)
        x = max(0, min(width - 1, x))
        cv2.line(overlay, (x, tick_top), (x, tick_bottom), RULER_COLOR, LINE_THICKNESS)
        label = RULER_LABELS.get(degrees)
        if label:
            cv2.putText(
                overlay,
                label,
                (max(2, x - 14), tick_top - 4),
                FONT,
                FONT_SCALE,
                RULER_COLOR,
                FONT_THICKNESS,
                cv2.LINE_AA,
            )

    corridor_top = height // 2
    for index, distance_m in enumerate(CORRIDOR_DISTANCES_M):
        half_angle = _corridor_half_angle(distance_m)
        color = CORRIDOR_COLORS[index]
        for side in (-1.0, 1.0):
            x = max(0, min(width - 1, angle_to_x(side * half_angle, width)))
            cv2.line(overlay, (x, corridor_top), (x, height - 1), color, LINE_THICKNESS)
        label_x = max(2, angle_to_x(half_angle, width) - 4)
        cv2.putText(
            overlay,
            f"body @{distance_m:g}m",
            (label_x, height - 4),
            FONT,
            FONT_SCALE,
            color,
            FONT_THICKNESS,
            cv2.LINE_AA,
        )

    blended = cv2.addWeighted(overlay, 0.85, image, 0.15, 0)
    ok, encoded = cv2.imencode(".jpg", blended, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        return jpeg_bytes
    return encoded.tobytes()
