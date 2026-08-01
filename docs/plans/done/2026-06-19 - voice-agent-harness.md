# Voice Agent Harness Plan

Goal: add a real iterative goal runner to the voice system without replacing the
working STT -> LLM -> TTS path.

This is not a memory plan, macro plan, generated-code plan, or robot-brain
rewrite. Those can become tools later. This plan only builds the core harness
that lets the model compose any available tools until a goal is done, blocked,
interrupted, or out of budget.

The first proof scenario is:

> "Hey, please move toward me and stop when you are close."

This is an example, not the design target. The design target is broader: the
robot should be able to use whatever tools it has to accomplish the user's goal.
For the proof scenario, it should be able to start a goal, observe, call tools such as
`look_around`, `inspect_robot`, `face_me`, `move_forward`, and later
`inspect_speaker_direction`, then keep looping until it decides it is close or
cannot continue.

Follow `AGENTS.md`: keep the implementation plain. No generic plugin framework,
service layer, dependency-injection container, planner hierarchy, or speculative
agent architecture. Build the smallest loop that proves the behavior.

## Runtime Shape

```text
Scribe commit
    |
    v
existing assistant turn
    |
    +-- normal answer / normal one-shot tool path
    |
    +-- start_goal(goal)
            |
            v
      voice agent runner
            |
            +-- optional narration, spoken only when the speaker is idle
            +-- tool calls through the same robot tool handlers
            +-- observations appended to task state
            +-- repeat until done / blocked / interrupted / budget
```

`start_goal` is not a normal robot action. It is a handoff from the normal
assistant turn into the agent runner. Once the handoff happens, the current
assistant turn should stop producing final text. The agent runner owns progress
narration and the final spoken result.

Implementation decision: active goals live in their own `active_goal` state
inside the voice orchestration loop, not inside the existing `ActiveTurn`.
`start_goal` should make the normal assistant turn finish quickly with a handoff
result. `handle_scribe_events()` then starts and owns the longer-lived goal task.
The existing `ASSISTANT_TURN_TIMEOUT_SECS` applies to normal assistant turns, not
to the whole goal.

## Non-Goals

- Do not add memory, filesystem wiki tools, macros, or generated tools here.
- Do not move this into `robot-brain` yet. Start inside the voice package so it
  can reuse the existing OpenAI client, TTS path, speaking flag, cancellation,
  tool callers, and tests.
- Do not hardcode behaviors like "approach user" or "approach wall". The model
  chooses tool calls. The harness owns iteration, budgets, observations, and
  speech timing.
- Do not add a second router LLM call before every response. The existing first
  assistant call chooses whether to call `start_goal`.
- Do not solve DoA staleness as part of the harness. A stale speaker direction
  is a normal tool observation. The model can ask the user to speak again, use a
  different tool, continue with lower confidence, or stop blocked.

## Core Invariants

- Tool calling and speech are independent.
- Narration must never overlap with existing robot speech.
- If `assistant_speaking` is true, tool completion is not an opportunity to
  narrate.
- If a tool completes while `assistant_speaking` is false, the runner may ask the
  model for new narration before the next tool call.
- Physical tools still go through the existing motion service and safety gates.
- User barge-in cancels the active goal promptly.
- User speech while a goal is active and the robot is silent is still meaningful:
  committed speech cancels the active goal first. If the committed speech is an
  explicit interrupt like "stop", the session returns to listening. If it is a
  real new request, the old goal is cancelled and the new request is processed.
- Progress narration uses the same playback path and `assistant_speaking` status
  as normal assistant speech.
- Agent observations can be plain structured text or multimodal message parts.
  Image tools are allowed in V1 only if the runner can pass real image inputs
  into the next model call.
- Image handling must work in both paths: quick one-off assistant tool calls and
  iterative agent tool calls. Do not regress the existing `look_around` behavior
  while adding the agent observation path.
- Goal turns may use a different reasoning/latency profile than normal chat.
  Quick conversation should stay low-latency; the agent loop is allowed to spend
  more thinking time when choosing the next action.
- A goal must end with one of: `done`, `blocked`, `cancelled`, `timeout`, or
  `step_limit`.

## Phase 1 - Share The Existing Tool Surface

### Goal

Make the current tools callable from both the normal assistant path and the new
agent runner without duplicating the long dispatch block in `voice/assistant.py`.

### Work

Add `src/voice/tools.py` with small data shapes and one dispatch function:

- `RobotToolCall`
  - `name`
  - `arguments`
  - `call_id`
- `RobotToolResult`
  - `name`
  - `call_id`
  - `ok`
  - `output`
- `AgentObservation`
  - text observation data for normal tool results
  - optional model input parts for tools that produce images
- `VoiceToolContext`
  - existing callables already threaded through `VoiceSession`

Move the implementation for these existing tools into that file:

- `switch_voice`
- `end_session`
- `wiggle`
- `move_forward`
- `look_around`
- `inspect_robot`
- `face_me`

Keep the public OpenAI tool definitions near the dispatcher or in
`voice/assistant.py`, whichever makes the first patch smaller. Do not build a
dynamic registry yet.

Add one small observation tool if it stays simple:

- `inspect_speaker_direction`
  - Calls the existing DoA snapshot path.
  - Returns whether the direction is fresh and the relative angle if known.
  - Does not move the robot.

This tool is useful for "move toward me", but the agent core must still work
without it.

### Tests

Add or extend tests around the dispatcher:

- Built-in motion tools call the existing motion intent caller.
- `look_around` can produce an image observation in the same model-input shape
  the current assistant expects.
- The image-input plumbing must move with the shared tool surface. The agent
  runner will not call `stream_openai_words()`, so it cannot rely on the current
  image path hidden inside the normal function-call flow.
- Existing quick one-off `look_around` behavior still passes an image to the
  follow-up model call.
- Agent-loop `look_around` passes the image as a multimodal observation to the
  next agent model call.
- `inspect_robot` returns the existing summarized telemetry shape.
- `face_me` still defers physical motion until playback is released in the
  normal assistant path.
- `inspect_speaker_direction` returns fresh/stale/unavailable from a fake DoA
  snapshot.

### Acceptance

```bash
python3 -m unittest tests.test_motion_intent tests.test_voice_core
```

Existing voice tool behavior should not change.

## Phase 2 - Add `start_goal` As A Handoff Tool

### Goal

Let the first existing assistant call decide when a user request needs the
iterative agent runner, without adding a separate router request.

### Work

Add a `start_goal` OpenAI tool:

```json
{
  "name": "start_goal",
  "description": "Start an iterative goal when the user asks for something that may require repeated tool use, observation, searching, checking progress, or working for more than one step.",
  "parameters": {
    "type": "object",
    "properties": {
      "goal": {"type": "string"}
    },
    "required": ["goal"],
    "additionalProperties": false
  },
  "strict": true
}
```

Update the operational prompt:

- Use `start_goal` for goals that need iteration.
- Do not use `start_goal` for simple conversation or one-shot actions.
- Examples:
  - "move forward" -> `move_forward`
  - "move forward until you are close to something" -> `start_goal`
  - "face me" -> `face_me`
  - "move toward me and stop when close" -> `start_goal`

Represent the handoff with a tiny internal value, for example `AgentGoalRequest`.
When `stream_openai_words()` sees `start_goal`, it should yield that value and
return instead of sending a function-call output back to the model.

`run_assistant_turn()` should return enough information for the session to know
whether it completed normally or handed off to an agent goal. Keep this local and
explicit; do not introduce a broad turn result hierarchy.

The handoff signal needs to bubble through the current stack:

```text
stream_openai_words
  -> run_assistant_turn
  -> ActiveTurn task result
  -> assistant_done event
  -> handle_scribe_events starts active_goal
```

Do not run the whole goal loop inside the existing `ActiveTurn` task. The
`ActiveTurn` should complete once the handoff is known.

### Tests

Add tests proving:

- `start_goal` produces a goal request and no spoken assistant chunks.
- The handoff result reaches `handle_scribe_events()` through the
  `assistant_done` event.
- The normal assistant path still speaks direct text.
- A normal one-shot tool still feeds its result back to the model.
- A model that calls `start_goal` and also emits text does not cause double
  speech. The goal handoff wins.

### Acceptance

```bash
python3 -m unittest tests.test_voice_core tests.test_motion_intent
```

## Phase 3 - Add Goal Lifecycle And Cancellation State

### Goal

Make active goals a first-class voice orchestration state before building the
loop itself.

### Work

Add a small `ActiveGoal` state owned by `handle_scribe_events()` or a nearby
plain helper:

- `goal`
- `user_text`
- `task`
- `stop_event`
- `started_at`
- `final_text`
- `terminal_reason`

Add it to `TurnRuntimeState` as `active_goal`.

When an `assistant_done` event carries an `AgentGoalRequest`:

- The normal `ActiveTurn` is complete.
- Create an `ActiveGoal`.
- Start the agent runner task.
- Publish status `thinking`.
- Do not append conversation history yet.
- Skip the normal `maybe_commit_history()` path for the handoff turn. The
  exchange should be committed only after the goal reaches a terminal result.

Cancellation rules:

- Session shutdown cancels `active_goal`.
- A committed user utterance while `active_goal` exists cancels that goal before
  processing the utterance.
- If that committed utterance is an explicit interrupt such as "stop", "wait",
  or "cancel", return to listening and do not start a new turn.
- If that committed utterance is a real new request, cancel the old goal and then
  process the new request normally.
- During progress speech, fast playback barge-in must work even though progress
  narration is not an `ActiveTurn`. The current code keys that path on
  `active_turn.is_playing_back()`, so Phase 5 must generalize the interruptible
  playback state to include goal progress playback.
- Between narrations, goal cancellation is driven by the committed-speech rule,
  not by playback-gated barge-in.

Goal completion rules:

- On `done` or `blocked`, speak the final result and append one history exchange.
- On `cancelled`, do not append progress narration as history.
- On `timeout` or `step_limit`, speak a short final result explaining that the
  goal stopped because of the limit.

### Tests

Add tests around the orchestration, not the model:

- A handoff creates `active_goal` and clears the normal active turn.
- Normal `ASSISTANT_TURN_TIMEOUT_SECS` does not wrap the whole goal task.
- The handoff `assistant_done` path does not call `maybe_commit_history()`.
- A committed "stop" while the goal is waiting on a fake tool cancels the goal
  and returns to listening.
- A partial or commit "stop" while goal progress speech is playing triggers the
  fast playback interrupt path, not only the slower committed-speech rule.
- A committed new request while a goal is active cancels the goal and starts the
  new request.
- Session shutdown cancels `active_goal`.
- Goal completion appends exactly one conversation exchange.

### Acceptance

```bash
python3 -m unittest tests.test_voice_core tests.test_voice_session
```

## Phase 4 - Implement The Agent Runner

### Goal

Create the loop that repeatedly asks the model what to do next, executes tools,
records observations, and stops only on a real terminal condition.

### Work

Add `src/voice/agent_runner.py`.

Start with one async function:

```python
async def run_agent_goal(
    goal: str,
    openai_client,
    model: str,
    reasoning: dict,
    tools,
    speak_progress,
    is_speaking,
    stop_event,
    max_steps: int = 20,
    max_seconds: float = 120.0,
) -> str:
    ...
```

The exact signature can follow the code once Phase 1 exists. Keep it direct.
The important part is that the goal runner has its own model/reasoning settings,
separate from the normal low-latency assistant turn.

Each loop iteration sends the model:

- Original goal.
- Current step number and remaining budget.
- Tool results so far.
- Any image observations as real model image inputs.
- Latest final/narration text already spoken.
- Tool descriptions.
- Rule: return structured JSON for the next action.

Use a simple structured response:

```json
{
  "narration": "I am checking where you are.",
  "tool_calls": [
    {"name": "face_me", "arguments": {}}
  ],
  "done": false,
  "blocked": false,
  "final": null
}
```

Rules:

- `done=true` requires `final`.
- `blocked=true` requires `final`.
- If both are false, at least one tool call is expected unless narration is being
  spoken before the next observation.
- Invalid JSON or invalid tool names are observations for the next iteration up
  to a small retry count, then `blocked`.
- Tool results are appended as observations, not hidden.
- Image results are appended as multimodal observations, not base64 stuffed into
  a JSON string.

Do not use private chain-of-thought fields. The runner needs observable state,
not hidden reasoning.

### Observation Shape

The runner should treat observations as model input, not only as JSON strings.

V1 needs two observation kinds:

- Text observations: tool name, arguments, ok/error, and structured output.
- Image observations: tool name, short text label, and an `input_image` part for
  the next Responses call.

`look_around` is allowed in V1 only if this multimodal observation path exists.
If that path is not implemented, remove `look_around` from the V1 agent tool list
until it is.

### Tool Execution

Execute tool calls in the order the model gives them.

For V1, run them sequentially. It is easier to reason about speech, motion, and
camera snapshots this way.

Await each tool result before starting the next tool or the next model
iteration. This matters for motion: `move_forward` is a small timed intent and
the motion intent bridge rejects concurrent intents as busy. Inspecting robot
state before the motion intent returns can read stale telemetry.

After any physical tool, append either:

- the tool's own result, and
- a fresh `inspect_robot` result when available,

or make the model explicitly call `inspect_robot`. Prefer the explicit model
call first. If manual testing shows the model often forgets to check after
motion, add an automatic post-motion observation as a later small patch.

### Tests

Use fake model responses and fake tools. Cover:

- Repeated `move_forward` until a later fake observation makes the model return
  `done`.
- A fake `look_around` result is sent to the next model call as an image input,
  not serialized into JSON.
- The existing normal assistant `look_around` path still sends the image to the
  follow-up model call.
- Motion tools are awaited before the runner starts the next model iteration or
  calls `inspect_robot`.
- Tool failure becomes an observation and the model can recover.
- Unknown tool name produces an observation, not a crash.
- Step limit returns a spoken final explaining it stopped.
- Timeout returns a spoken final explaining it stopped.
- `stop_event` cancels the goal.

### Acceptance

```bash
python3 -m unittest tests.test_voice_agent_runner
```

## Phase 5 - Add Progress Narration Without Overlap

### Goal

Let the agent narrate longer-running work while guaranteeing speech and tools do
not trample each other.

### Work

Add a progress speaker owned by `VoiceSession`.

The runner may request narration only at these points:

- Before executing the next batch of tools, if `assistant_speaking` is false.
- After a tool batch completes, only if `assistant_speaking` is false.
- At final completion, after pending tool execution has finished.

The runner must not request narration while a tool call is running. That keeps
the rule simple:

```text
maybe narrate
execute tool batch
maybe narrate
execute next tool batch
final
```

The progress speaker should:

- Set the same speaking state used by normal TTS.
- Use the same playback path as normal assistant speech.
- Use its own playback lifecycle and playback id; do not pretend narration is
  part of the old `ActiveTurn`.
- Register itself as the current interruptible playback source while it is
  playing. The barge-in checks in `handle_partial()` and `handle_commit()` should
  ask for the current interruptible playback, instead of checking only
  `active_turn.is_playing_back()`.
- Use that same current-interruptible-playback accessor everywhere the voice
  loop internally derives "assistant is speaking" from active turn playback,
  including partial handling, commit handling, audio-activity barge-in
  accumulation, and status/barge-in telemetry. Do not leave parallel
  active-turn-only and progress-playback-only notions of speaking.
- Emit the same `speaking` phase/status events that normal TTS emits, with a
  goal/progress event type when useful for the dashboard.
- Return only after playback has finished or been cancelled.
- Respect barge-in cancellation.
- Call `stop_playback_now()` promptly when cancellation arrives during progress
  speech.

If a tool finishes while `assistant_speaking` is true for any reason, skip
narration for that boundary and continue the loop.

### Tests

Use fake speaker state and fake tools. Cover:

- Narration before a tool sets speaking on, then off.
- A tool is not started until narration finishes.
- Narration is skipped when `is_speaking()` is true.
- Partial "stop" during progress speech stops playback promptly.
- Committed "stop" during progress speech cancels the goal and returns to
  listening.
- Tool completion while speaking does not trigger narration.
- Final speech does not overlap with progress speech.
- Cancellation while progress speech is playing stops the goal.

### Acceptance

```bash
python3 -m unittest tests.test_voice_agent_runner tests.test_voice_session
```

## Phase 6 - Finish VoiceSession Wiring

### Goal

Connect the handoff, goal lifecycle, agent runner, tool dispatcher, and progress
speaker into one working path.

### Work

In `VoiceSession.start()`, pass the agent runner the same things the assistant
already uses:

- OpenAI client.
- OpenAI model.
- voice state / TTS voice.
- existing tool callables.
- status callback.
- event callback.
- stop event.

When an assistant turn returns a goal handoff:

- Start `active_goal` through the Phase 3 lifecycle path.
- Publish status `thinking` while it is deciding.
- Publish status `speaking` only while progress or final TTS is playing.
- Append one conversation exchange when the goal finishes:
  - user text: original committed transcript
  - assistant text: final agent result

Do not append progress narration as separate assistant exchanges.
Do not append every tool result to normal conversation history. For V1, the
final answer should include enough context that the next user can understand why
the goal stopped. A compact goal trace can be stored later.

### Tests

Add voice-session level tests with fake OpenAI, fake TTS, and fake tools:

- A committed transcript that triggers `start_goal` launches the runner.
- The final agent result is committed to history once.
- Barge-in cancels an active goal.
- Committed speech while the goal is silent cancels the goal.
- Session shutdown cancels an active goal.
- Normal replies still use the old path.

### Acceptance

```bash
python3 -m unittest tests.test_voice_core tests.test_voice_session tests.test_voice_agent_runner
```

## Phase 7 - Manual Robot Validation

### Goal

Prove the harness works on the real robot with only current tools plus the
optional speaker-direction observation tool.

### Scenarios

Run these with dashboard telemetry visible.

1. Simple response:
   - User: "hello"
   - Expected: no agent goal, normal low-latency response.

2. One-shot motion:
   - User: "wiggle"
   - Expected: no agent goal, one motion tool call.

3. Iterative approach:
   - User: "please move toward me and stop when you are close"
   - Expected:
     - assistant calls `start_goal`
     - agent optionally narrates before work
     - agent calls some mix of `inspect_speaker_direction`, `face_me`,
       `look_around`, `inspect_robot`, and `move_forward`
     - agent keeps looping after one move
     - agent stops when it believes it is close or when motion/safety data says
       it cannot continue
     - agent treats stale DoA as an observation and chooses a next step, such as
       asking the user to speak again, using another available tool, or stopping
       blocked
     - agent prefers the motion stack's `drive.safety_blocked` /
       `drive.safety_reason` over inventing a raw distance threshold when those
       fields are available
     - no narration overlaps with another narration or tool execution

4. Barge-in during goal:
   - User starts the approach goal, then says "stop"
   - Expected:
     - progress speech or current task stops promptly
     - no further motion tools are called
     - session returns to listening

5. Tool failure:
   - Disable or fake one tool, then ask an iterative goal.
   - Expected:
     - failure is fed back as an observation
     - agent either chooses another tool or returns a blocked final answer

### Acceptance

The iterative approach should show the harness working, not hardcoded movement
logic: the logs show more than one agent loop iteration, tool results are fed
back as observations, stale/unavailable tool results do not crash the goal, and
the final answer explains why the goal stopped.

## Future Consumers

After this plan is complete, memory, macros, filesystem search, and narrow
generated tools can be added as new tools. They should not require changing the
agent loop. They only need to provide clear tool descriptions and structured
results.
