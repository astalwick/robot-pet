# Face Detection Plan

Goal: add Raspberry Pi-local face detection and draw face boxes on the web dashboard camera feed.

This plan is written for handoff. Follow the stages in order. Do not redesign the service boundaries unless a stage proves impossible.

## Decisions Already Made

- V1 is face detection only. No face identity, no face matching, no image storage.
- Future face matching may be remote/offloaded, so keep the detector easy to replace.
- Add a new `robot-vision` service. Do not put face detection in `robot-camera`.
- `robot-camera` remains the only service that opens the Pi camera.
- `robot-vision` polls `robot-camera` snapshots from `http://127.0.0.1:8081/snapshot.jpg`.
- Use OpenCV on CPU with Haar cascade for V1.
- Install OpenCV from apt with `python3-opencv`.
- Publish face boxes through the existing telemetry stream.
- Face boxes are normalized coordinates from `0.0` to `1.0`.
- Detection defaults to enabled at `2.0 Hz`.
- Configurable detection rate range is `0.2 Hz` to `10.0 Hz`.
- Disabling vision leaves `robot-vision.service` running but idle. It must not poll snapshots or run OpenCV while disabled.
- Vision config is web-dashboard only for V1. Do not add it to the SSH TUI yet.

## Stage 1 - Add Vision Config

Add a small persistent config module for vision settings.

Implementation:

- Create `src/config/vision.py`.
- Store config at `/home/pi/.config/robot-pet/vision.json`.
- Add a frozen dataclass, likely `VisionConfig`, with:
  - `enabled: bool = True`
  - `detection_rate_hz: float = 2.0`
- Clamp `detection_rate_hz` to `0.2..10.0`.
- Add load/save helpers matching the simple style of `src/config/teleop.py`.
- Use atomic save with temp file plus `os.replace`, like drive tuning.
- Treat missing config as defaults.
- Raise a clear config error for malformed JSON or non-object JSON.

Tests:

- Add `tests/test_vision_config.py`.
- Test missing file returns defaults.
- Test save/load round trip.
- Test malformed JSON raises the config error.
- Test non-object JSON raises the config error.
- Test rate clamping.
- Test boolean parsing through `from_dict`.

Acceptance:

- `python -m unittest tests.test_vision_config` passes.
- No service code imports OpenCV in this stage.

## Stage 2 - Add Vision Telemetry Shape

Extend existing telemetry so `robot-vision` can publish small JSON updates and the dashboard can receive them through `/events`.

Implementation:

- In `src/telemetry/messages.py`, add `vision_update(...)`.
- Use source name exactly `vision`.
- Vision update payload should include:
  - `enabled`
  - `status`
  - `faces`
  - `image_width`
  - `image_height`
  - `detection_rate_hz`
  - `last_detection_time`
  - `error`
- In `src/robot_telemetry.py`, include `vision` in `sources`.
- Include top-level `vision` in snapshots, using the latest `vision` source data.
- Keep telemetry generic. Do not create a special vision socket.

Payload contract:

```json
{
  "type": "source_update",
  "source": "vision",
  "time": 1770000000.0,
  "enabled": true,
  "status": "detecting",
  "faces": [
    { "x": 0.12, "y": 0.20, "width": 0.18, "height": 0.24 }
  ],
  "image_width": 1280,
  "image_height": 720,
  "detection_rate_hz": 2.0,
  "last_detection_time": 1770000000.0,
  "error": null
}
```

Allowed `status` values:

- `disabled`
- `detecting`
- `camera_unavailable`
- `detector_unavailable`
- `error`

Tests:

- Add or extend telemetry message tests for `vision_update`.
- Extend telemetry hub tests so snapshots include:
  - `sources.vision`
  - top-level `vision`
  - stale status when no vision update has arrived
  - non-stale status after a vision update

Acceptance:

- Existing gamepad/system telemetry behavior is unchanged.
- `python -m unittest tests.test_telemetry_messages tests.test_robot_telemetry` passes.

## Stage 3 - Add The Robot Vision Service

Create the new service entrypoint. Keep it plain and small.

Implementation:

- Create `src/robot_vision.py`.
- Add CLI args:
  - `--config`, default `/home/pi/.config/robot-pet/vision.json`
  - `--camera-url`, default `http://127.0.0.1:8081/snapshot.jpg`
  - `--telemetry-socket`, default publish socket
- Use `urllib.request` or another standard-library HTTP client for snapshots. Do not add a new HTTP dependency.
- Decode JPEG bytes with OpenCV.
- Use OpenCV Haar cascade for face detection.
- Convert pixel boxes to normalized `{x, y, width, height}`.
- Publish a vision telemetry update after each detection attempt.
- When config says `enabled: false`:
  - publish `status: "disabled"` occasionally, about once per second is fine
  - do not fetch snapshots
  - do not run the detector
  - sleep and check config mtime again
- Check the config file mtime about once per second.
- Only reload config when mtime changes.
- If the config file is missing, use defaults.
- If config is invalid, keep the last good config, publish `status: "error"`, and keep running.
- If camera snapshot fetch fails, publish `status: "camera_unavailable"` and keep running.
- If OpenCV or the Haar cascade cannot load, publish `status: "detector_unavailable"` and keep running without crashing repeatedly.

Detector notes:

- Keep the OpenCV-specific code in one small class or a few nearby functions inside `robot_vision.py`.
- Do not build a detector plugin system.
- Do not add face matching hooks yet.
- It is fine to make a tiny named function for box normalization because tests need it.

Tests:

- Add `tests/test_robot_vision.py`.
- Use fake config paths, fake snapshot fetchers, fake detector functions, and fake telemetry publishers.
- Test disabled mode does not fetch snapshots.
- Test enabled mode fetches and publishes faces.
- Test normalized box math.
- Test invalid config keeps last good config.
- Test camera fetch failure publishes `camera_unavailable`.
- Test detector unavailable publishes `detector_unavailable`.

Acceptance:

- Tests do not require real camera hardware.
- Tests do not require OpenCV to be installed, except for any import path that is deliberately skipped when missing.
- `python -m unittest tests.test_robot_vision` passes.

## Stage 4 - Wire The Web Dashboard Overlay

Draw boxes over the existing MJPEG image.

Implementation:

- Update `src/web_dashboard_static/index.html`.
- Wrap the camera image in a positioned frame or add an overlay inside `#camera-section`.
- Keep the existing `img#camera-stream`.
- Add an overlay element, for example `div#face-overlay`.
- Update `dashboard.css` so the overlay sits above the image and does not block pointer events.
- Update `dashboard.js`:
  - read `snapshot.vision`
  - render one box per face
  - clear boxes if `sources.vision.stale` is true
  - clear boxes if `vision.last_detection_time` is more than about 2 seconds older than `snapshot.time`
  - account for the visible image rectangle when `object-fit: contain` creates letterboxing
- Use normalized boxes against the visible image area, not the full camera section.

Box drawing rule:

- Let the image container be `containerWidth x containerHeight`.
- Let source image size be `image_width x image_height`.
- Compute the contained image rectangle using the source aspect ratio.
- Draw boxes inside that contained rectangle.
- If source image dimensions are missing, clear boxes.

Tests:

- Extend `DashboardJsTest` or add static assertions that:
  - overlay element exists
  - stale vision logic exists
  - existing camera URL still uses `window.location.hostname`
- If practical, add a small browser-level test later, but do not make Playwright a new project dependency for this stage.

Acceptance:

- Existing dashboard camera stream still loads from `http://${window.location.hostname}:8081/stream.mjpg`.
- Boxes disappear within about 2 seconds after vision stops publishing.
- Boxes line up on desktop and mobile layouts because letterboxing is handled.

## Stage 5 - Add Web Vision Config UI

Expose vision settings in the web dashboard Config modal only.

Implementation:

- In `src/robot_web_dashboard.py`, add a separate vision config path argument:
  - `--vision-config`, default `/home/pi/.config/robot-pet/vision.json`
- Add handlers:
  - `GET /config/vision`
  - `POST /config/vision`
- GET returns fields and values for:
  - `enabled`
  - `detection_rate_hz`
- POST saves only `vision.json`.
- POST must not restart `gamepad-teleop`.
- Update `dashboard.js` so opening Config loads both drive config and vision config.
- Keep one OK button for the modal.
- On submit, save drive config and vision config with their existing endpoints.
- If vision save succeeds but drive save fails, show the drive error. Do not hide the modal.
- If drive save succeeds but vision save fails, show the vision error. Do not hide the modal.
- Keep the UI simple: checkbox for enabled, numeric input for rate.

Tests:

- Extend `tests/test_robot_web_dashboard.py`.
- Test `GET /config/vision`.
- Test `POST /config/vision`.
- Test vision POST writes the config file.
- Test vision POST does not call or require `restart_gamepad_teleop`.
- Keep existing drive config tests passing.

Acceptance:

- Web config can enable/disable vision without restarting any service.
- Applying drive config still restarts only `gamepad-teleop`.

## Stage 6 - Add Systemd, Setup, And Redeploy Wiring

Make the service deployable on the Pi.

Implementation:

- Add `systemd/robot-vision.service`.
- It should run as `User=pi`.
- It should set `PYTHONPATH=/home/pi/robot-pet/src`.
- It should run `/home/pi/robot-pet/.venv/bin/python /home/pi/robot-pet/src/robot_vision.py`.
- It should restart always with a short restart delay.
- It should start after and want `robot-camera.service` and `robot-telemetry.service`.
- Update `setup.sh`:
  - install `python3-opencv`
  - add sudoers permission to restart `robot-vision.service`
  - enable `robot-vision.service`
  - restart `robot-vision.service`
- Update dashboard/redeploy log tail lists to include `robot-vision`.
- Update `README.md` and `docs/ARCHITECTURE.md` to list `robot-vision`.
- Update `pyproject.toml` `py-modules` to include `robot_vision`.

Tests:

- Run the full unit test suite.
- Check service file syntax by inspection.

Acceptance:

- Fresh setup installs OpenCV and starts the vision service.
- Redeploy restarts vision with the rest of the robot services.
- Operator logs include vision service logs.

## Stage 7 - Manual Pi Validation

Run this after code is deployed to the Raspberry Pi.

Validation steps:

1. Open `http://<pi-host>:8080/`.
2. Confirm the camera stream still works.
3. Open Config, confirm Vision fields appear.
4. Set Vision enabled, rate `2.0`, press OK.
5. Put a face in view.
6. Confirm a box appears over the face.
7. Move around and confirm the box updates at roughly the configured rate.
8. Disable Vision in Config.
9. Confirm boxes disappear and CPU drops back down.
10. Re-enable Vision and try `0.2`, `2.0`, and `10.0 Hz`.
11. Check logs:
    - `journalctl -u robot-vision -f`
    - no repeated crash loop
    - no repeated config read spam
12. Stop `robot-camera.service` temporarily and confirm vision reports camera unavailable without crashing.

Acceptance:

- Face boxes draw over live camera video.
- Disabling vision stops camera polling and inference work.
- Camera or detector errors are visible in telemetry/logs but do not crash the dashboard.
- Robot driving services remain unaffected by vision failures.

## Final Full Test Command

Run before handing off:

```bash
python -m unittest discover tests
```

## Non-Goals For This Plan

- No face recognition.
- No remote face matching.
- No image upload.
- No saved face crops.
- No new frontend framework.
- No ROS2 migration.
- No SSH TUI vision config.
- No detector plugin framework.
