# Reliability / Observability Plan

Keep this small and AGENTS.md-friendly: add facts, counters, and clear logs before adding new architecture.

## First - implemented 2026-05-09

- [x] Add explicit drive status telemetry from `gamepad_teleop`.
  - Current state: waiting for controller, waiting for RoboClaw, driving, stopped, controller lost, motor command failed.
  - Last stop reason.
  - Controller reader alive.
  - Last command acknowledgement age.
  - Consecutive motor command failures.

- [x] Separate motor command health from optional telemetry read health.
  - Command acknowledgement failures are safety-relevant.
  - Encoder/battery/current read failures are observability-relevant.

- [x] Count telemetry publish drops.
  - Keep publish best-effort.
  - Expose failed publish count and last publish status in telemetry.

- [x] Improve transition logs.
  - Log drive state changes and stop reasons in one clear line.
  - Avoid a state-machine framework unless the simple version becomes hard to follow.

## Next

- Add a plain boot diagnostics script.
  - Check controller device.
  - Check RoboClaw version/readiness.
  - Check telemetry sockets.
  - Check camera health.
  - Show relevant systemd unit states and recent fault logs.

- Give camera capture a simple recovery policy.
  - After repeated capture failures, mark unhealthy and either stop for restart-on-demand or retry after a cooldown.
  - Keep logging rate-limited if failures are persistent.

## Later / Only If Needed

- Dashboard fault timeline.
  - Useful after drive status and transition logs exist.
  - Keep it as recent events, not a new event framework.

- Broader readiness checks for every service.
  - Prefer existing logs, health endpoints, or simple files under `/run/robot-pet/`.
  - Do not add a health server just to make everything symmetrical.
