> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Self-Created Tools Plan

## Goal

Let Bloop extend its own capabilities: define a new tool during a conversation,
give it a name, and call it later — including in a future session.

Seed example: the user teaches the robot "a little dance," the robot defines a
named tool that strings together moves it already knows, and later "do your
dance" calls it.

## How tools work today

The assistant's tools are a static list, `ASSISTANT_TOOLS` in
`src/voice/assistant.py` (`wiggle`, `move_forward`, `look_around`, `face_me`,
`camera_snapshot`, `inspect_robot`, `end_session`, ...). Each tool's handler is a
caller threaded down from `RobotVoiceService` through `VoiceSession` into the
turn handling. Anything touching motion goes out over the motion-intent socket to
`robot-motion`, which is the motion boundary (bounded, gamepad-preemptible, drops
on lost readiness).

Self-created tools means making that list dynamic.

## Candidate shapes

### A. Named macros (composition of existing tools)

A new tool is a stored list of existing tool calls with fixed arguments:
`dance = [wiggle, look_around, wiggle]`. Stored as data, not code. Exposed back
to the model as a callable that replays the sequence through the existing
callers. No new capability beyond what the built-in tools already do.

### B. Parameterized macros

Same as A, but the macro takes an argument the model fills in (e.g.
`spin(degrees)` mapping to a bounded turn). Arguments need validation and
clamping like the existing intents.

### C. Generated code

The LLM writes Python that runs on the Pi. This is a different capability class
from A/B: it can do things no existing tool exposes, including driving hardware
outside the `robot-motion` path. Would need a sandbox and a defined capability
surface.

## Persistence and the model's view

A self-created tool is only useful if it survives a restart — overlaps with
[[memory]] (a learned tool is a durable fact about what the robot can do).
Storage would land next to other runtime state (e.g. under
`/home/pi/.config/robot-pet/`).

For the model to call a tool, it has to see it. Two pieces:

- A `define_tool` (and maybe `forget_tool`) tool the model invokes to create one.
- Merging the stored tools into the tool list presented each turn.

## Brainstorm — directions

- **Define by demonstration.** Capture the sequence of tool calls the model just
  made this turn and offer to save it as a named macro ("save that as 'dance'") —
  no separate authoring step.
- **Reuse the existing callers.** A macro replays through the same validated tool
  handlers, so per-step bounds, timeouts, and gamepad preemption are inherited
  rather than reimplemented.
- **Store with memory.** Persist defined tools in the same store as [[memory]] —
  a learned tool is a durable fact about what the robot can do.
- **Preview before run.** List the steps a macro will execute on first use / on
  operator approval, so an unfamiliar macro isn't a black box.
- **Macros of macros, bounded.** Allow a macro to call another, with a depth and
  total-step cap so a definition can't expand without limit.
- **Redefine with history.** Saving over an existing name overwrites but keeps the
  previous version, so a bad redefinition is recoverable.
- **Dashboard surface.** List defined tools in the web dashboard for review and
  deletion, mirroring how personality/voice state is already surfaced.

## Open questions

1. **Which shape(s)** — composition only (A/B), or generated code (C)?
2. **Approval.** Does `define_tool` take effect immediately, or does the operator
   confirm (e.g. via the dashboard) first?
3. **Naming.** Collisions with built-in tools or with each other — reject,
   override, or namespace?
4. **Replay semantics.** Run macro steps sequentially through existing callers,
   honoring each step's timeout and gamepad preemption? What happens on a
   mid-sequence failure?
5. **Bounds.** Cap on number of stored tools and steps per tool?
6. **Hardware reach.** For C, what is the capability surface and does motion still
   route through `robot-motion`?
7. **Is this the LLM's job?** A macro could instead be an operator feature in the
   dashboard. Is "robot defines its own tool" the goal, or "robot has teachable
   routines"?
