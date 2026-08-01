# Async Nav Goals (post-ROS2)

**Status: parked until the ROS2 migration and lidar/slam_toolbox/nav2 land.**
This is the "next important thing" note, not a detailed plan. It exists so the
design constraint is on record before anyone tries to bolt `navigateTo` onto
the motion-intent socket.

## The problem

Every LLM motion tool today is a blocking request/reply: voice sends an intent
over the Unix socket and waits up to `MOTION_INTENT_REPLY_TIMEOUT_SECONDS`
(35 s) for the motion to finish. That contract is right for a 2 m move or a
360° turn. It is wrong for navigation: a cross-house `navigateTo('kitchen')`
can take minutes, and the LLM should be able to talk, observe, and — above all
— **stop** while the robot is en route.

nav2 already has the correct model: **actions** (send goal → returns
immediately; poll feedback/status; cancel). The work is exposing that model to
the LLM without making robot-voice a ROS process.

## Shape of the solution

1. **A nav-bridge node** in `src/ros_nodes/` — the third use of the
   motion-intent socket pattern, so the abstraction is now earned. It is an
   rclpy node holding a nav2 action client, serving a Unix socket with small
   JSON verbs:
   - `navigate_to(place | pose)` → sends the nav2 goal, replies immediately
     with a goal id.
   - `nav_status()` → active/succeeded/aborted, distance remaining, current
     pose in `map`.
   - `nav_cancel()` → cancels the active goal, replies when nav2 confirms.
   - `create_place(name)` → TF lookup `map → base_link` now, saved to a places
     JSON (requires map persistence — see the lidar-readiness checklist in the
     [ros2-migration plan](2026-07-10%20-%20ros2-migration.md)).
2. **New LLM tools** in `voice/tools.py`: `navigate_to`, `check_navigation`,
   `create_place` — plus `stop` grows a nav-cancel: a voice "stop" must cancel
   the active nav goal, not just the local motion intent.
3. **Goal-runner changes**: the iterative agent runner treats an in-flight
   navigation as something to poll between model calls, not something to block
   on — send the goal, then observe via `check_navigation` (and camera) each
   iteration until arrival. The normal assistant turn probably only gets
   `navigate_to` + `stop`; long supervision belongs to the goal runner.
4. **Arbitration prerequisite**: this only works once `/cmd_vel` arbitration
   (`twist_mux` + intent ranking) from the lidar-readiness checklist is
   decided — otherwise nav twists and motion intents fight.

## Explicitly not designing yet

Recovery behaviors, multi-goal queues, per-goal timeouts, and whether
`face_me`/`express` should pause or abort navigation. Decide those with a
working nav stack in hand.
