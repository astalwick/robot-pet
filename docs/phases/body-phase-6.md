# Body Phase 6 — Docking and Self-Recharge

## Goal

Let the robot find a dock, align with it, make electrical contact, and recharge itself.

This is intentionally later than navigation. It is both a navigation problem and an electrical/mechanical design problem.

## Entry Criteria

- Body Phase 5 autonomous navigation works well enough to reach approximate dock area.
- The robot's power architecture is ready to evolve beyond manual charging.
- The chassis can accept charging contacts or a dock alignment mechanism.

## Exit Criteria

This phase is done when:

- The robot can decide it needs to charge or accept a "go dock" command.
- The robot can navigate near the dock.
- The robot can perform close-range alignment.
- Charging contacts engage reliably.
- Charging state is detectable by software.
- The robot can safely leave or remain docked based on charge state.

## Default Direction

- Treat docking as a custom build, not an off-the-shelf product.
- Expect a beacon or close-range alignment aid.
- Expect contact pads and a dock housing, likely with 3D-printed parts.
- Revisit the robot power architecture before exposing the dock to routine use.

## Cross-Track Dependencies

- Personality Phase 6 can use docking state as a self-maintenance behavior.
- Speech/personality can make docking feel alive, but the docking logic itself should be deterministic.

## Not In Scope

- Manipulation.
- Whole-home semantic task planning.
- Untethered multi-day autonomy.
- Fancy dock UX before reliable electrical contact.

## Notes

This is likely harder than it looks. Keep it late, and break it down: reach dock area, close-range detection, alignment, contact, charging state, autonomous charge routine.
