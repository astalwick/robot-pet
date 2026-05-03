# Body Phase 4 — SLAM and Localization

## Goal

Give the robot a map and the ability to localize itself within that map.

This is the phase where the project likely becomes a real robotics stack instead of a collection of Python services.

## Entry Criteria

- Body Phase 0 is complete.
- Body Phase 2 safety sensing is good enough to protect experiments.
- The chassis can carry the mapping sensor with a rigid mount.
- Odometry from the drivetrain is usable enough to feed the mapping stack.

## Exit Criteria

This phase is done when:

- The robot can build a map of a test area.
- The robot can localize itself on that map after restarting.
- Mapping data can be inspected with standard tooling.
- The robot can be manually driven while mapping.
- The map is stable enough to become the basis for navigation.

## Default Direction

- Use ROS2 when this phase starts in earnest.
- Use Nav2-compatible concepts and tooling unless there is a strong reason not to.
- Prefer lidar for the first serious SLAM attempt because it is the straightforward path.
- Keep the MacBook available for heavier tooling or visualization if useful.

## Cross-Track Dependencies

- Named places in the personality layer come after this phase. The map exists first; labels like "kitchen" are annotations.
- Personality Phase 2 may expose tiny movement tools before this, but `navigate_to()` should not exist until this phase and Body Phase 5 provide the underlying capability.

## Not In Scope

- Autonomous navigation to named rooms.
- Docking.
- Semantic map understanding.
- Long-term map memory beyond what is needed for localization.

## Notes

This phase is a likely architecture turning point. The earlier "drivers survive, services are scaffolding" rule exists so the project can move into ROS2 without rewriting the hardware layer.
