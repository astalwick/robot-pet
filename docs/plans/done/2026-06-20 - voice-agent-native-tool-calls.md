# Voice Agent Native Tool Calls Plan

Goal: replace the agent runner's hand-written JSON action protocol with normal
OpenAI tool calls, and shape the loop so it reasons and operates the way a good
agent harness does (Claude Code style): the model's reasoning is preserved across
steps, the harness stays out of the model's way, the goal ends naturally, and
speech runs in parallel with tool execution.

This plan follows `docs/plans/2026-06-19 - voice-agent-harness.md`. The harness
exists now, but the iterative runner asks the model for a custom JSON object with
`tool_calls`, `done`, `blocked`, and `final`. The normal assistant path already
uses native OpenAI tool calls. The agent runner should use the same model-facing
tool mechanism.

This is not a compatibility migration. There is no partial deploy path and no
old protocol to preserve. Put the new code on the robot.

Follow `AGENTS.md`: keep this plain. Do not build a framework, router layer,
planner abstraction, or generalized tool registry.

## Current Problem

The normal assistant turn calls OpenAI with `ASSISTANT_TOOLS` and handles
`function_call` output items. The agent runner does something different: it asks
the model to emit text JSON, parses it with `json.loads`, validates tool names
itself, and then dispatches through `voice.tools.dispatch_tool`.

That split creates two tool protocols for the same robot:

- One-off assistant turns: native OpenAI tool calls.
- Iterative agent goals: hand-written JSON pseudo tool calls.

The split makes tool discovery, argument validation, and future tool expansion
weirder than they need to be. It also makes the loop reason worse than it should:
each step the runner rebuilds a flat transcript and re-injects bookkeeping, so the
model never gets to carry a coherent train of thought across steps.

## What "Reasons And Operates Well" Means Here

Established harnesses (Claude Code, Codex) share four properties we want:

1. The model's reasoning is preserved across the whole loop, so step N builds on
   the thinking from step N-1 instead of re-deriving a plan from a flat log.
2. The harness gives the model tools and a good operating prompt, then gets out of
   the way. It does not nag the model with bookkeeping every turn.
3. The goal ends naturally: when the model stops asking for tools, its text is the
   answer.
4. Output does not block work. The model keeps acting while it talks.

This plan adopts all four for the robot, adjusted for the one real constraint a
physical robot adds: tool execution is serialized (one motion at a time), but
speech is not — the robot can and should talk while it moves.

## Target Shape

```text
start_goal(goal)
    |
    v
agent runner loop  (own reasoning thread via previous_response_id)
    |
    +-- Responses call with native tools, parallel_tool_calls=False
    |     - robot tools: move_forward, turn, wiggle, face_me,
    |       inspect_robot, look_around, inspect_speaker_direction
    |
    +-- if the response has no tool call: speak its text and finish (natural end)
    +-- otherwise execute exactly one tool call
    +-- launch optional narration as a concurrent task (never blocks the tool)
    +-- send function_call_output back via previous_response_id
    +-- append image observations as real input_image parts
    +-- repeat until natural end, cancellation, timeout, or step limit
```

The runner owns iteration, budgets, cancellation, speech timing, and tool
execution order. The model owns choosing the next tool, narrating, and deciding
the goal is done.

## Conversation Continuity And Reasoning

This is the most important change and the foundation for the rest.

The runner MUST chain calls with `previous_response_id`, exactly like the normal
assistant path (`assistant.py` passes `previous_response_id` and then sends only
the new tool outputs). Do not keep rebuilding a manual `input_items` transcript.

Why: with `previous_response_id`, OpenAI preserves the model's reasoning items
between tool calls server-side. The model carries its thinking forward across the
whole goal instead of starting cold each step. This is the direct analog of a
harness threading thinking blocks back through its loop, and it is the single
biggest lever for making the goal loop reason well.

Concretely:

- First call: `input = [developer prompt, "Goal: <goal>"]`, `tools=...`. Capture
  `response.id`.
- Each later call: `previous_response_id=<last id>`, and `input` is just the new
  material for this step:
  - the `function_call_output` for the tool that just ran, and
  - if that tool produced an image, a follow-up `{"role": "user", "content":
    [...input_image...]}` message.
- Capture the new `response.id` each step for the next call.

Switching to `previous_response_id` also resolves, for free, the two things a
manual transcript would get wrong: echoing the model's `function_call` items back
before their outputs, and threading reasoning items between turns.

This relies on the default `store=True`, the same as the assistant path.

## Termination

End the goal the way Claude Code does: the loop runs while the model keeps asking
for tools. When a response comes back with no tool call, the model is done. There
is no terminal tool and no done/blocked flag — the model just stops calling tools,
and its final sentence carries the result to the user.

- Loop while the response contains a `function_call`.
- If a response has no tool call and non-empty assistant text, that text is the
  final answer. Speak it and finish. Do not nag. Whether the goal succeeded or the
  robot is giving up, the model expresses it in plain words ("I'm right next to you
  now" vs "I couldn't get past the chair, so I stopped"). The harness does not need
  a structured success/failure field.
- Only nudge the model when a response has no tool call AND no usable text, i.e.
  an empty response. Cap nudges with a small retry count, then finish.

Do not treat "the model emitted text and no tool call" as a mistake to correct.
Punishing the model for thinking out loud is the opposite of operating well.

A native response can carry assistant text *and* a function call in the same turn.
The assistant text plays one of two roles, decided solely by whether a tool call is
present:

- Tool call present -> the text is progress narration. Speak it through
  `speak_progress` and nothing else. Do not commit it to history, and do not treat
  it as the final. If it cannot play (the robot is already speaking), drop it
  silently — no queue, no history.
- No tool call -> the text is the final. Speak it (awaited) and commit it to history
  the way `finish_goal` already does.

Only the final reaches history. Mid-step narration is spoken and forgotten.

The runner logs its own terminal reason via `log.info` at each exit point
(`natural`, `timeout`, `step_limit`, `cancelled`), and the model never sees it.
Do not over-read this: it is plain text logging, not structured telemetry. The
runner returns a bare final string, so `finish_goal` records `terminal_reason =
"done"` and the `goal_done` event carries no reason — natural success, timeout,
and step limit are indistinguishable at the event layer, and nothing currently
needs them to be. Only the cancel path sets a distinct reason today. If a consumer
ever needs to tell these apart in telemetry, that is a deliberate result-shape
change (return the reason alongside the text); do not build it on spec now.

## Native Agent Tools

The agent runner uses the existing shared robot tool definitions. The current
`AGENT_TOOLS` list already includes all of these:

- `move_forward`
- `turn`
- `wiggle`
- `face_me`
- `inspect_robot`
- `look_around`
- `inspect_speaker_direction`

Keep `turn` in the list. The earlier draft of this plan dropped it; the robot
needs to turn to face a direction, and it is already a shared tool.

There is no terminal tool. The model finishes by replying with a final sentence
and no tool call (see Termination).

Set `parallel_tool_calls` to `false`. This is a physical-safety constraint, not a
reasoning limitation: the motion intent bridge rejects concurrent intents, and
inspecting robot state mid-motion reads stale telemetry. One decision, one tool,
observe, decide again. Do not "optimize" this into parallel tool batches later
without revisiting the motion serialization.

## Agent Prompt Changes

Replace the JSON-action prompt with a richer native-tool operating prompt. A thin
prompt is the main reason a loop feels aimless, so spell out how to operate:

- You are a small physical robot pet working toward a goal over several steps.
  Your motion is real, timed, and happens in the world.
- Use the tools to act and observe. Call one tool at a time. You get each tool's
  result back before you choose again.
- After any motion (`move_forward`, `turn`, `face_me`), call `inspect_robot` or
  `look_around` to observe the result before deciding the goal is finished. Do not
  assume a move succeeded.
- Prefer the motion stack's own signals: when deciding whether you are blocked or
  close to something, trust `drive.safety_blocked` / `drive.safety_reason` from
  `inspect_robot` over inventing a raw distance threshold.
- A failed tool is an observation, not the end. Try a different tool or finish
  blocked. Do not repeat the same failing action.
- You may speak short progress updates with assistant text while you work. Keep
  spoken text easy to say out loud: no symbols, lists, or markdown.
- Finish by replying with a short final sentence and no tool call. Say what
  happened in plain words: that you reached the goal, or that you could not and
  why. Do not keep calling tools once you are done.

Do not ask the model to return JSON. The only structured outputs are native tool
calls.

## Reasoning Effort

Keep `AGENT_REASONING_EFFORT` at `"low"` for now. The goal loop is the deliberate
path and could justify more thinking later, but low keeps steps responsive on the
robot. Keep it a single named constant so it is easy to raise if manual testing
shows the loop choosing poorly.

## Speech Runs In Parallel With Tool Execution

Progress narration must NOT block tool calls. The robot should be able to speak
while it moves. The only thing forbidden is two speeches overlapping.

Today narration is awaited to completion before the next tool runs, which makes
the robot act, then speak, then act, then speak in sequence. Change this:

- Progress narration is launched as a concurrent task (fire-and-forget). The tool
  loop proceeds immediately; tool execution and narration playback overlap.
- At most one narration plays at a time. Before launching narration, skip it if
  the robot is already speaking. Guard the launch with a local in-flight flag set
  synchronously, not only with `is_speaking()`, so two narrations cannot race into
  flight before the first registers as playing.
- Tool execution never waits on narration. Motion and speech overlapping is
  desired, and the motion stack already serializes motion against motion.
- The final result is different: it must be heard. Speak the final synchronously
  (awaited). Before speaking the final, cancel or let finish any in-flight progress
  narration so the final never overlaps a progress line.
- Cancellation and barge-in must stop both at once: a committed "stop" cancels the
  goal (`stop_event`) and stops any in-flight narration playback. Keep the existing
  interruptible-playback wiring (`state.progress` / `current_playback()`), now with
  a tool possibly running concurrently.
- Narration must register into the assistant echo-memory window when it finishes.
  `is_recent_assistant_echo` (in `handle_scribe_events`) suppresses STT echo by
  matching against `state.active_turn` / `state.progress`, falling back to the
  time-windowed `recent_assistant_text` / `recent_assistant_echo_until`. Turns set
  that fallback; `speak_progress` does not. With the goal still running after a
  narration line clears `state.progress`, a delayed STT echo of that line would
  match nothing, read as user speech, and barge-in to cancel the live goal. When
  narration playback completes, record its text into `recent_assistant_text` and
  `recent_assistant_echo_until` (using `assistant_echo_memory_secs`) the same way a
  turn does. This is a change in `voice/assistant.py` `speak_progress`, not in the
  runner.
- The narration task is detached, so handle it like one: attach a done-callback
  that logs any exception (a fire-and-forget task that raises is otherwise
  swallowed silently), and hold its handle so every goal exit — natural end,
  timeout, step limit, cancellation, and an unexpected error — awaits or cancels
  it in a `finally`. Never leave a dangling narration task when the goal ends,
  including when the goal raises.

Revised loop shape:

```text
get next decision  (previous_response_id chained)
  no tool call?  -> speak final text (awaited), finish
  robot tool?    -> maybe launch narration task (concurrent)
                    execute the one tool (awaited)
                    send function_call_output, append image obs
                    loop
```

This supersedes the Phase 5 rule in the harness plan that narration and tool
execution must not overlap. Speech-vs-speech still must not overlap; speech-vs-tool
now may.

## Behavior To Preserve

- `start_goal` remains the handoff from the normal assistant turn.
- The normal one-off assistant tool path is not changed.
- Progress narration is spoken only when no other speech is playing.
- Progress narration never overlaps another narration or the final.
- User barge-in cancels the active goal promptly, including mid-narration and
  mid-tool.
- A delayed STT echo of the robot's own narration does not cancel the goal; it is
  suppressed by the assistant echo-memory window even after `state.progress` clears.
- Each physical tool is awaited before the next model decision.
- Tool failures become observations and the model can recover.
- Unknown or unsupported tool calls become observations, not crashes, even though
  native tools make them rare.
- `look_around` image results are sent back as real image input parts, not as
  base64 inside JSON text.
- Timeout and step limit still produce spoken final messages.
- The goal ends with one of these internal reasons (logging only): natural,
  cancelled, timeout, or step limit.

## Implementation

Update `src/voice/agent_runner.py`.

The model call should pass:

- `tools=AGENT_TOOLS`
- `parallel_tool_calls=False`
- `reasoning={"effort": AGENT_REASONING_EFFORT}` with the constant at `"low"`
- `previous_response_id` on every call after the first
- `input` limited to the new step material (first call: developer prompt + goal;
  later calls: the `function_call_output` and any image follow-up message)

The agent runner stays non-streaming: a single `responses.create` per step whose
`response.output` items are read after the call returns. Only the normal assistant
turn streams. The goal loop deliberates between physical actions, so there is no
partial-token UX to stream, and reading whole output items keeps the loop simple.

The runner should read the response output items:

- Find `function_call` items. With `parallel_tool_calls=False` there is at most
  one; if more appear, execute the first and feed back an observation that only one
  tool runs per step.
- Collect assistant text. Its role depends on whether a function call is present
  (see Termination): with a tool call it is progress narration (spoken only, never
  history, never the final); with no tool call it is the final.
- If there is no function call:
  - non-empty text -> speak it (awaited) and finish.
  - empty text -> nudge up to the retry cap, then finish.
- Otherwise dispatch the robot tool through `voice.tools.dispatch_tool`. Native
  `function_call.arguments` is a JSON string; parse it with
  `parse_tool_arguments(...)` the same way the assistant path does. Mirror this in
  the test fakes (arguments is a string, not a dict).
- Send the tool result back as `function_call_output` with the matching `call_id`.
- If the tool result has image parts, append them as a user message so the next
  model call sees real image input.

Remove the per-step bookkeeping the model used to see. Do not inject a
"Step N of M, about X seconds left, respond with JSON" user message every
iteration, and do not inject a "wrap up soon" note near the limit. Keep `max_steps`
and `max_seconds` as outer guardrails the model never sees, enforced by the harness
(step counter and `asyncio.wait_for`). If a limit hits, speak the existing canned
final. Otherwise stay silent so the loop reasons on a clean context.

Set `max_steps = 60` and `max_seconds = 120.0`. These are runaway guards, not a
budget the goal is expected to use; they are named constants, easy to change.

Prune the now-dead JSON scaffolding: malformed-JSON handling, the JSON code-fence
stripping, and the unknown-tool-name branch are unreachable with native strict
tools. Keep only the empty-response nudge with its retry cap.

Keep the current timeout, step-limit, and cancellation behavior. A "step" is one
model decision plus any tool execution from that decision.

Keep the shared dispatcher in `src/voice/tools.py`. Do not duplicate robot tool
implementation in the runner.

## Tests

Rewrite `tests/test_voice_agent_runner.py` around native response output items
instead of JSON strings. The fakes must expose `response.output` as a list of
items with `.type == "function_call"`, `.name`, `.arguments` (a JSON string), and
`.call_id`, plus `response.id` for chaining and a way to script plain-text
responses for natural termination.

Cover the native-tool mechanics:

- The agent model call includes native tools and `parallel_tool_calls=False`.
- `move_forward` is executed from a native `function_call`, with `arguments`
  parsed from the JSON string.
- Tool output is sent back as `function_call_output` with the matching `call_id`.
- `look_around` image output is sent to the next model call as `input_image`, not
  serialized into JSON.
- Tool failure is returned as an observation and the model can call a different
  tool or finish.
- Unknown or unsupported tool names become observations, not crashes.

Cover the "reasons and operates well" properties:

- Continuity: each call after the first passes `previous_response_id`, and the
  input carries only the new `function_call_output` (and image message), not the
  whole transcript.
- Natural termination: a response with no tool call and non-empty text finishes the
  goal and speaks that text, with no nudge.
- Empty response with no tool call nudges up to the cap, then finishes.
- No per-step bookkeeping or "wrap up soon" message is injected on a normal
  multi-step run.

Cover speech concurrency:

- Assistant text arriving alongside a function call is narrated, not committed to
  history and not treated as the final; the final (text with no tool call) is the
  only text that reaches history.
- Narration is launched concurrently and does not block the next tool: a tool
  starts while narration is still "playing" in a fake speaker.
- Two narrations never overlap: narration is skipped while the robot is speaking.
- The final is spoken awaited and never overlaps an in-flight progress narration.
- A committed "stop" cancels the goal and stops in-flight narration.
- A delayed STT echo of a narration line, arriving after `state.progress` has
  cleared while the goal still runs, is suppressed as assistant echo and does not
  cancel the goal. (This test lives with the `handle_scribe_events` /
  `speak_progress` echo-memory change in the assistant tests, not the runner.)

Keep timeout, step limit, and cancellation tests.

Also keep or adjust the existing normal assistant tests that prove one-off tools
still use `ASSISTANT_TOOLS` and that `start_goal` remains a handoff.

Acceptance:

```bash
python3 -m unittest tests.test_voice_agent_runner tests.test_voice_core tests.test_voice_session
```

## Manual Validation

Run after tests pass:

1. Ask: "move forward."
   - Expected: normal one-off path still calls `move_forward`, not the agent
     runner.

2. Ask: "move toward me and stop when close."
   - Expected: normal assistant calls `start_goal`; agent runner then uses native
     robot tool calls, narrates while it moves (speech overlaps motion, not other
     speech), and finishes with a plain final sentence and no tool call.

3. Ask a goal that requires looking around.
   - Expected: `look_around` result is visible to the next model call as an image
     input and the goal continues.

4. Disable or fake one tool failure.
   - Expected: failure is fed back as a tool observation and the agent either tries
     another tool or finishes by saying it could not continue.

5. Barge-in mid-narration while moving.
   - Expected: "stop" cancels the goal and stops the narration promptly; no further
     motion; session returns to listening.

## Future Work

After the runner uses native tool calls, revisit
`docs/plans/2026-06-19 - agent-tool-discovery.md`.

At that point, tool discovery should prefer native schema discovery where it fits
the runner. A generic `call_tool` wrapper should not be added unless there is a
separate reason native tool calls cannot cover the tool set.
