# Body Phase 5 — Autonomous Navigation

## Goal

Let the robot move itself to map goals without direct gamepad control.

This is the first phase where "go there" becomes a real body capability instead of a personality-layer illusion.

## Entry Criteria

- Body Phase 4 SLAM and localization are working.
- Body Phase 2 safety sensing is integrated enough to stop unsafe motion.
- The robot has a usable map of at least one test area.
- Manual gamepad takeover remains available.

## Exit Criteria

This phase is done when:

- The robot can accept a map goal and plan a path to it.
- The robot can execute that path at conservative speed.
- The robot can stop or recover when blocked.
- Navigation success, failure, and progress are visible to the operator.
- The capability can be wrapped as a tool such as `navigate_to_pose()` or `navigate_to_marker()`.

## Default Direction

- Use ROS2 / Nav2 for navigation.
- Keep high-level navigation commands separate from low-level motor control.
- Start with explicit map goals before named places.
- Add named-place labels only after the map and localization are reliable.

## Cross-Track Dependencies

- Personality Phase 2 can expose toy movement tools earlier, but real navigation tools wait for this phase.
- Personality Phase 6 becomes more interesting once this exists because the robot can initiate meaningful movement.
- "This room is the kitchen" belongs after this phase starts working, as a map annotation or memory feature.

## Not In Scope

- Docking and charging.
- Manipulation.
- Fully autonomous exploration without constraints.
- Letting the LLM stream velocity commands.

## Notes

The LLM should request navigation. The navigation stack should plan, execute, and report back. The body runtime remains the authority on whether motion is safe.
