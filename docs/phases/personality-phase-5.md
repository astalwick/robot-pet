# Personality Phase 5 — Expression Display

## Goal

Reflect personality and emotional state through an onboard expression display.

This phase makes the robot feel more present even when it is not speaking.

## Entry Criteria

- Personality Phase 3 or Phase 4 has enough character direction to know what should be displayed.
- Display hardware has been chosen and mounted.
- The Pi can drive the display without interfering with existing robot control.

## Exit Criteria

This phase is done when:

- The robot can show simple expressions.
- Expressions can change in response to conversation, tool calls, or emotional state.
- The display has a small vocabulary of useful states.
- The display does not block or destabilize motion control.

## Default Direction

- Treat the display as an output device for the personality layer.
- Start with simple expressions rather than elaborate animation.
- Keep the expression API high-level, such as `set_expression("curious")`.
- Preserve deck and lidar space when mounting display hardware.

## Cross-Track Dependencies

- Benefits from Personality Phase 4 emotional state.
- Does not require autonomous navigation.
- Body mounting choices should account for future lidar in Body Phase 4.

## Not In Scope

- Full face animation system.
- Computer vision from the display.
- Making expression hardware mandatory for earlier personality phases.

## Notes

The shopping list originally treats the LED face as deferrable. That still fits: add it when personality work wants a visible output channel.
