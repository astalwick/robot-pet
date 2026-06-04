# ReSpeaker DoA Calibration and `face_me` Plan

## Goal

Use the ReSpeaker XVF3800 direction-of-arrival value to let the robot turn toward
the person who just spoke.

The first version should make one slow, bounded, gamepad-preemptible turn. It
should not continuously chase live DoA while moving.

## Hardware Test

Tested on:

- ReSpeaker USB device VID `0x2886`, PID `0x001e`
- Diagnostic:

  ```bash
  sudo .venv/bin/python scripts/diagnostics/respeaker-flex-python-control/respeaker_get_doa.py --interval 0.1
  ```

The diagnostic currently needs `sudo` because the `pi` user does not have
permission to open the USB control interface. Add a udev rule before production
use so `robot-voice` can read DoA without running as root.

## Measured Orientation

Robot-relative position names refer to the physical robot:

| Speaker position | Stable raw DoA |
| --- | ---: |
| Front bumper | `270-271` |
| Beside left drive wheel | `356` |
| Beside right drive wheel | `180` |
| Rear caster, slightly toward left drive wheel | `83-85` |

Treat the clean conceptual orientation as:

```text
front bumper:      270
left drive wheel:    0
rear caster:        90
right drive wheel: 180
```

Convert raw DoA to a signed robot-relative angle with:

```python
relative_degrees = ((raw_doa - 270 + 180) % 360) - 180
```

This produces:

```text
front:        0
left wheel:  positive
right wheel: negative
rear-left:   approximately +174
```

Positive means turn toward the left drive wheel. Negative means turn toward the
right drive wheel.

## Observed DoA Behavior

DoA becomes very stable after the beamformer settles:

- Front settled at `270-271`.
- Left settled at `356`.
- Right settled at `180`.
- Rear-left settled at `83-85`.

The beginning of a new speech direction can be wrong or transitional. Examples
observed before settling included:

- Front speech wandering through `185`, `268`, `296`, `306`, and `324` before
  settling at `270-271`.
- A new right-side test initially remaining at `27`, then moving through `121`,
  `161`, `174`, and `178` before settling at `180`.
- Rear-left moving through `14`, `45`, `60`, `71`, and `81` before settling at
  `83-85`.

The reported angle remains cached after `SPEECH_DETECTED` becomes `0`.
Production code must ignore DoA samples unless speech detection is active.

The diagnostic prints a five-byte-looking result such as:

```text
[0, 14, 1, 1, 0]
```

Its current interpretation is:

```python
raw_doa = result[1] + result[2] * 256
speech_detected = bool(result[3])
```

The vendored `xvf_host.py` describes `DOA_VALUE` differently from
`respeaker_get_doa.py`. The diagnostic interpretation above was verified against
physical speaker positions and should be treated as correct for the installed
firmware.

## Proposed DoA Selection

Do not average an entire utterance because early transitional samples can pull
the result far away from the final speaker direction.

Initial proposed rule:

- Poll DoA at about `10 Hz` while voice is active.
- Only collect samples when `speech_detected` is true.
- Accept an angle after recent readings remain within about `5 degrees` for at
  least `0.5 seconds`.
- Cache the most recent stable angle.
- Reject a cached angle older than about `2 seconds` when `face_me` executes.
- Treat relative angles within about `15 degrees` as already facing the speaker.

These thresholds are starting points, not calibrated final values.

## Proposed `face_me` Flow

```text
user speaks
  -> robot-voice polls and caches stable DoA
  -> LLM calls face_me
  -> robot-voice converts cached raw DoA to signed relative angle
  -> robot-voice sends bounded face_me intent with angle to robot-motion
  -> robot-motion performs one slow timed rotation
```

`robot-motion` remains the motion and safety boundary. The turn must:

- Be slow and time-bounded.
- Be preempted by gamepad input.
- Stop if the motion service loses readiness.
- Avoid raw motor or continuous velocity control from the LLM.

Do not continuously follow DoA while turning. The user will usually be silent by
then, the angle becomes stale, and motor noise may affect the microphone array.

## Remaining Calibration

Before implementing the timed turn:

1. Use the diagnostic-only bounded one-direction turn intent. It is not exposed
   as an LLM tool:

   ```bash
   python scripts/diagnostics/motion-timed-turn.py toward_left_wheel 0.5
   python scripts/diagnostics/motion-timed-turn.py toward_right_wheel 0.5
   ```

2. Measure robot rotation for several short durations at a conservative angular
   command.
3. Choose a simple duration-per-degree estimate.
4. Cap large rear turns so an inaccurate timed turn cannot spin indefinitely.
5. Later, replace timed angle estimation with BNO085 heading feedback when the
   IMU is installed.
