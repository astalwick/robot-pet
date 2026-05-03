# Body Phase 7+ — Expansion Platform

## Goal

Use the robot as a base for open-ended tinkering: arms, richer embodiment, new sensors, new compute, and experiments that are not yet predictable.

The long-term goal is not a fixed product spec. It is a platform that keeps being fun to modify.

## Entry Criteria

- The robot is physically and architecturally stable enough that additions do not constantly break the base.
- Power, mounting, wiring, and software extension points are understood.
- Earlier phases have created enough capability to make new hardware interesting.

## Exit Criteria

There is no single exit. Each expansion should define its own small deliverable.

Examples:

- Add an arm that can pick up simple objects.
- Add richer expression hardware.
- Add a different drive base.
- Add more sensors.
- Add onboard acceleration or a future Jetson-class computer.
- Add mechanical features that make it feel more pet-like.

## Default Direction

- Preserve modularity.
- Prefer additions that bolt onto the existing goBILDA ecosystem or use clean mounting points.
- Keep drivers isolated and testable.
- Avoid turning one experiment into a permanent architectural burden unless it proves useful.

## Cross-Track Dependencies

- Personality phases can make new body features expressive.
- Body expansions should expose high-level tools to the personality layer only after the hardware behavior is bounded and understood.

## Not In Scope

- Pretending the roadmap can predict every future experiment.
- Optimizing early phases for hardware that may never be added.

## Notes

This phase is the reason the earlier phases should stay clean. The platform should invite "what if I add..." rather than punish it.
