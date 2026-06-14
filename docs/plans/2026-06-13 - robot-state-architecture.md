# Robot State Architecture Bootstrap Plan

Goal: establish a working skeleton for events, interpreted current state,
conversation history, reflection, durable knowledge, prompt context, and behavior
orchestration.

This plan implements the decisions in
`docs/plans/2026-06-13 - robot-state-architecture.decisions.md`. It is not an
attempt to finish memory, mood, presence, identity, or proactive behavior. Each
phase should leave behind one real path that can be inspected and extended.

Follow `AGENTS.md`: keep services thin, domain logic plain, and changes direct.
Do not introduce a generic framework for messages, stores, workers, or commands.

## Resulting Runtime Shape

```text
robot-motion / sensors / vision / voice
              |
              v
      existing robot-telemetry
              |
              v
          robot-model --------> robot-events --------> robot-brain
              ^                      |
              |                      v
        affect updates        robot-reflection
              ^                      |
              |                      v
          robot-memory <------ conversation journal
              ^
              |
          robot-voice
```

New systemd processes:

- `robot-events`
- `robot-model`
- `robot-memory`
- `robot-reflection`

`robot-brain` remains its existing process but gains observe-only policy logic.

## Shared Constraints

- Existing telemetry, motion safety, battery policy, and command sockets remain
  unchanged.
- New IPC uses newline-delimited JSON over Unix sockets.
- Event delivery is transient, broadcast-all, and fire-and-forget.
- Voice continues working when every new service is unavailable.
- Domain modules do not import systemd or ROS2 libraries.
- Generated data lives under `/home/pi/.local/share/robot-pet/`.
- Tests use temporary sockets, directories, and SQLite files.

## Phase 1 - Transient Event Bus

### Goal

Create the live event path used by model transitions, conversation lifecycle,
reflection triggers, and brain policy.

### Work

Add `src/events/messages.py`:

- Build and validate the common envelope:

  ```json
  {
    "type": "presence_changed",
    "time": 1781395200.0,
    "source": "robot-model",
    "data": {}
  }
  ```

- Require `type`, `time`, `source`, and object-valued `data`.
- Keep validation limited to external input shape.

Factor the two low-level socket primitives out of `src/telemetry/socket_client.py`
into a shared module (for example `src/lib/jsonl_socket.py`): a fire-and-forget
line-publish and a reconnecting line-subscribe. Update telemetry to use them so the
boilerplate is not duplicated.

Add `src/events/socket_client.py` as thin contract wrappers over those primitives:

- `publish_event(...)` sends once and does not await acknowledgement.
- `subscribe_events(...)` yields events and reconnects on a fixed interval.
- Do not copy the socket boilerplate; only the event message contract lives here.

Add `src/robot_events.py`:

- Listen on separate publisher and subscriber sockets.
- Broadcast each valid event to every connected subscriber.
- Do not retain events or send a snapshot on subscriber connection.
- Remove dead subscribers when writes fail.
- Publish basic service health through existing telemetry if useful; do not add
  event history to telemetry.

Add default socket paths to `src/telemetry/paths.py` or a small events-local paths
module, whichever keeps ownership clearest during implementation.

Add `systemd/robot-events.service` and include it in setup/redeploy/dashboard
service lists where existing services are enumerated.

### Tests

Add:

- `tests/test_event_messages.py`
- `tests/test_robot_events.py`

Cover envelope validation, fanout to multiple subscribers, subscriber removal,
no replay for late subscribers, and client reconnection.

### Acceptance

```bash
python3 -m unittest tests.test_event_messages tests.test_robot_events
```

## Phase 2 - Operational Store And Memory Service

### Goal

Persist committed conversation exchanges and establish the human-readable
knowledge wiki behind one memory API.

### Work

Add a small shared SQLite connection/schema module. It should:

- Open `robot.db` in WAL mode.
- Create only the tables needed by the current phase.
- Keep transactions short.
- Accept an injected path.
- Avoid a migration framework; a schema version plus direct upgrade functions is
  enough when an actual second schema appears.

Add `src/memory/store.py` with conversation operations:

- Insert one committed exchange with:
  - stable row ID
  - `session_id`
  - user text
  - assistant text
  - user/assistant or exchange timestamps
- Query exchanges within a configurable time window and newest-count cap.
- Query exchanges after a row ID for reflection.
- Do not store tool calls, partials, speculative turns, or cancelled turns.

Add `src/memory/knowledge.py`:

- Own Markdown below the configured `memory/` root.
- Start with global `self` and `world` documents.
- Read all global knowledge as structured document records.
- Accept a candidate fact, category, and provenance.
- Append the candidate to the target document as a new provenance-tagged entry.
  Do not call an LLM; appending cannot lose existing facts.
- Provenance records the timestamp, whether the candidate came from an explicit
  `remember` or from reflection, and any evidence journal row IDs.
- Write through a temporary file and replace the target atomically so a crash
  cannot corrupt the document.
- Use the same append path for explicit and reflection-derived candidates.
- Consolidating or deduplicating accumulated entries is a later manual/offline
  pass and is out of scope here.

Add `src/memory/client.py` and `src/robot_memory.py`:

- One request/reply socket with concrete operations:
  - `record_exchange`
  - `recent_exchanges`
  - `exchanges_after`
  - `read_global_knowledge`
  - `remember_fact`
  - `status`
- One request per connection.
- Permit callers to send and close for fire-and-forget writes.
- `robot-memory` needs no `OPENAI_API_KEY`; every operation, including
  `remember_fact`, works without credentials.

Add `systemd/robot-memory.service` with defaults:

```text
/home/pi/.local/share/robot-pet/robot.db
/home/pi/.local/share/robot-pet/memory/
```

### Tests

Add:

- `tests/test_memory_store.py`
- `tests/test_memory_knowledge.py`
- `tests/test_robot_memory.py`

Cover exchange insertion/query ordering, two-hour/forty-exchange bounds,
cross-session rows, global wiki reads, provenance-tagged append, crash-safe
atomic write, preservation of existing entries on append, and request dispatch.

### Acceptance

```bash
python3 -m unittest tests.test_memory_store tests.test_memory_knowledge tests.test_robot_memory
```

## Phase 3 - Current Robot Model

### Goal

Turn existing telemetry into one structured current model containing physical
self-state, stable anonymous presence, and persisted PAD affect.

### Work

Add plain domain modules under `src/model/`:

- `physical.py`
  - Consume existing semantic telemetry fields only.
  - Project battery status, motion availability/state, safety block/reason,
    camera/vision availability, and recent motion outcome.
  - Do not create voltage, distance, or safety thresholds.
- `presence.py`
  - Consume vision face-count observations.
  - Become present after 2 continuous seconds with a face.
  - Become absent after 10 continuous seconds without a face.
  - Produce transitions only when stable state changes.
- `affect.py`
  - Represent pleasure, arousal, and dominance with a neutral midpoint.
  - Apply bounded reflection deltas.
  - Add a bodily arousal contribution from concrete physical state.
  - Clamp and decay each axis toward neutral using configurable rates.
  - Restore affect from SQLite and apply elapsed wall-clock decay at startup.
  - Derive deterministic labels, intensity, and optional cause.
- `state.py`
  - Own the combined snapshot and update lifecycle.
  - Emit a new snapshot only when meaningful model state changes.

Add `src/model/client.py` and `src/robot_model.py`:

- Subscribe continuously to the existing telemetry snapshot socket.
- Feed physical and vision observations into the model.
- Publish transition events such as `presence_changed` through `robot-events`.
- Expose a dedicated model-state subscriber socket:
  - current snapshot on connect
  - later snapshots on change
- Expose a small request socket for `apply_affect_delta` and status reads.
- Checkpoint affect in model-owned SQLite tables.
- Publish compact health/debug telemetry, not the full model snapshot.

Add `systemd/robot-model.service`. It should soft-order after telemetry and events
but reconnect when either stream restarts.

### Tests

Add:

- `tests/test_model_physical.py`
- `tests/test_model_presence.py`
- `tests/test_model_affect.py`
- `tests/test_robot_model.py`

Use fake clocks. Cover physical projection, no competing thresholds, two-second
arrival, ten-second departure, flapping suppression, PAD clamp/decay, restart
decay, snapshot streaming, and presence events.

### Acceptance

```bash
python3 -m unittest \
  tests.test_model_physical \
  tests.test_model_presence \
  tests.test_model_affect \
  tests.test_robot_model
```

## Phase 4 - Voice History, Context, And Remember

### Goal

Make conversation continuous across voice sessions and give every turn current
embodied context plus durable global knowledge.

### Phase 4a - Write Path

Extend conversation records in `src/voice/conversation.py`:

- Carry timestamps and `session_id` where needed.

Add a committed-exchange callback at the point where
`handle_scribe_events(...)` currently appends a completed exchange:

- Fire-and-forget `record_exchange` to `robot-memory`.
- Publish a `conversation_activity` event to `robot-events`.
- Include the current voice session ID.
- Do not wait, retry, or maintain an outbox.

Publish `session_ended` when an active voice session ends.

Maintain a local cache of the `robot-model` snapshot in `RobotVoiceService`.
Model disconnection leaves the last snapshot available with suitable freshness
information; it must not stop conversation.

### Phase 4b - Prompt Composition

Load recent committed exchanges from `robot-memory` when a session starts:

- Defaults:
  - previous 2 hours
  - newest 40 exchanges
- Make both limits voice configuration.
- Render time gaps/session boundaries without turning them into fake user text.
- Avoid re-journaling history loaded from memory.

Change LLM input composition to:

```text
system: operational instructions + character + current global knowledge
prior user/assistant messages
system: ephemeral current physical state and affect
user: newest committed utterance
```

Details:

- Refresh global `self` and `world` knowledge for each committed turn.
- Keep the active/recent transcript as ordinary message history.
- Never store the ephemeral state system message in conversation history.
- State physical facts as authoritative grounding.
- Tell the model to let affect influence expression without announcing it by
  default.
- Omit unavailable model or memory context and continue the turn.

Add the `remember` tool:

- Use only for explicit user requests.
- Accept concise fact plus `self` or `world` category.
- Fire-and-forget `remember_fact` to `robot-memory`.
- Let voice respond immediately without reporting storage success.
- Log submission failure without changing the spoken response.

Re-scope the existing `inspect_robot` tool to an on-demand detailed diagnostics
view:

- Drop the summary fields now carried by the ephemeral per-turn state message so
  the two paths do not overlap.
- Keep it as the way to pull detail on request: pack voltage, per-sensor
  distances, SoC temperature, memory, disk, uptime, and throttling.
- Update the tool description so the model presents it clearly as an on-demand
  detailed diagnostics view rather than a general status summary.

### Tests

Update/add tests in:

- `tests/test_voice_core.py`
- `tests/test_voice_session.py`
- `tests/test_robot_voice.py`
- `tests/test_voice_config.py`

Cover cross-session history loading, configurable limits, time-gap rendering,
no duplicate journaling, best-effort failures, exact message ordering, ephemeral
state exclusion from history, per-turn knowledge refresh, explicit-only
`remember` behavior, and `inspect_robot` returning detailed diagnostics without
the fields now in the ephemeral state message.

### Acceptance

```bash
python3 -m unittest \
  tests.test_voice_core \
  tests.test_voice_session \
  tests.test_robot_voice \
  tests.test_voice_config
```

## Phase 5 - Background Reflection

### Goal

Interpret batches of completed conversation outside the voice hot path and
optionally update affect and durable knowledge.

### Work

Add `src/reflection/core.py`:

- Accept a batch of journal exchanges and current affect.
- Call configurable `gpt-5.4-mini` through a narrow model-caller boundary.
- Request structured output containing optional:
  - pleasure delta
  - arousal delta
  - dominance delta
  - short affect cause
  - zero or more candidate facts with category and evidence row IDs
- Permit an entirely empty result.
- Keep robot responses as context while instructing the model that human input is
  the source of social affect changes.

Add reflection progress storage:

- One cursor row owned by `robot-reflection` in `robot.db`.
- Read journal exchanges after the cursor through `robot-memory`.
- Advance the cursor only after the batch completes.
- On failure, keep the cursor unchanged and wait for the next normal trigger.

Add `src/robot_reflection.py`:

- Subscribe to `robot-events`.
- Reset a configurable idle timer on `conversation_activity`; default 60 seconds.
- On `session_ended`, schedule reflection after 10 seconds.
- At startup, perform one catch-up pass when journal rows exist after the cursor;
  log this as an error/abnormal recovery.
- Do not poll the journal while events are unavailable.
- Submit PAD deltas to `robot-model`.
- Submit candidate facts to `robot-memory` for provenance-tagged append.
- Start without credentials but leave LLM-backed reflection inactive and logged.

Add `systemd/robot-reflection.service` with soft ordering only.

### Tests

Add:

- `tests/test_reflection_core.py`
- `tests/test_robot_reflection.py`

Cover empty output, either/both outputs, malformed output, cursor advancement,
cursor preservation on partial failure, idle/session timers, startup catch-up,
and behavior while events are disconnected.

### Acceptance

```bash
python3 -m unittest tests.test_reflection_core tests.test_robot_reflection
```

## Phase 6 - Brain Stake In The Ground

### Goal

Turn `robot-brain` from an empty loop into the owner of cross-domain behavior
policy, without enabling autonomous speech or movement.

### Work

Add a small brain policy module that tracks in memory:

- latest committed-conversation time
- latest would-greet time
- latest presence state

Consume events:

- `conversation_activity`
- `presence_changed`

On stable anonymous arrival, record `would_greet=true` only when:

- no conversation occurred in the previous 30 seconds
- no would-greet decision occurred in the previous 5 minutes

Otherwise record the concrete suppression reason.

Update `src/robot_brain.py` to:

- subscribe to `robot-events`
- apply the policy
- log decisions
- publish compact brain status through telemetry
- never invoke voice or motion in this phase

### Tests

Add `tests/test_robot_brain.py` coverage for greeting eligibility, recent-chat
suppression, cooldown suppression, and restart-reset semantics.

### Acceptance

```bash
python3 -m unittest tests.test_robot_brain
```

## Phase 7 - Read-Only Operator View

### Goal

Make the new architecture inspectable before enabling autonomous behavior.

### Work

Extend `robot-web-dashboard` with read-only endpoints and UI for:

- latest model snapshot
- raw PAD and derived affect
- bounded recent live events held only by the dashboard process
- recent conversation journal rows
- reflection cursor, last pass, and last error
- current global wiki documents
- latest brain would-greet decision and suppression reason

Use existing service APIs and telemetry status. Do not let the dashboard read
SQLite directly, edit wiki files, replay events, approve memories, or inject
model state.

Keep the UI plain and diagnostic. Avoid building a general event explorer or
memory administration interface.

### Tests

Extend `tests/test_robot_web_dashboard.py` for endpoint behavior, unavailable
service handling, and safe rendering of model/wiki data.

### Acceptance

```bash
python3 -m unittest tests.test_robot_web_dashboard
```

## Phase 8 - Integration And Deployment

### Goal

Verify the walking skeleton as one system and ship it without weakening existing
robot behavior.

### Work

Add one process-level integration test using temporary sockets, SQLite, and wiki
paths:

```text
voice-style committed exchange
  -> robot-memory journal
  -> reflection batch with fake LLM
  -> optional PAD delta and candidate fact
  -> robot-model and wiki update
  -> voice context builder reads both
```

Add a second narrow integration path:

```text
vision telemetry flaps
  -> robot-model hysteresis
  -> one presence_changed arrival
  -> robot-brain would-greet decision
```

Update:

- `setup.sh`
- `restart.sh`
- `scripts/redeploy-robot.sh`
- service lists in dashboard code
- `docs/ARCHITECTURE.md`

Document the future ROS2 seam:

- Physical observations and embodied state may move to ROS2 nodes.
- Voice, reflection, and memory remain non-ROS Python services.
- No bridge is implemented now.
- Existing semantic clients/domain modules must not import ROS2.

Run the focused suites after each phase, then the full suite:

```bash
python3 -m unittest discover -s tests
```

### Pi Smoke Test

1. Start all services with no OpenAI credentials. Existing motion, sensors,
   camera, dashboard, and voice behavior still starts; reflection/merge report
   inactive state without crashing other services.
2. Restore credentials and restart only memory/reflection as needed.
3. Hold a short conversation, end the session, and reconnect within two hours.
   The new session receives the prior exchanges with a visible time gap.
4. Say, "remember that you are in Longueuil." The spoken response is immediate;
   the wiki later contains a new provenance-tagged entry for the fact.
5. Complete a conversation that produces no notable fact. Reflection runs and is
   allowed to make no changes.
6. Complete a conversation that clearly changes affect. Confirm PAD, derived
   labels, cause, and the next turn's ephemeral state context update.
7. Walk into and out of camera view while detections flap. Confirm one arrival
   after 2 seconds and one departure after 10 seconds.
8. Confirm brain logs a would-greet decision once, then suppresses repeats for 5
   minutes and suppresses arrivals within 30 seconds of conversation.
9. Stop `robot-events`. Existing voice and physical services continue; event-
   driven reflection/brain behavior pauses. Restart events and confirm subscribers
   reconnect for future events without replay.

### Scenario Eval (manual, not CI)

The unit tests with a fake LLM only exercise plumbing (apply, clamp, decay, cursor
advance). They do not show that reflection reacts to meaning. Add a small scenario
eval that runs by hand against the real `gpt-5.4-mini`, kept out of the CI suite:

- Feed fixed transcript fixtures (for example a sneer versus a compliment) through
  `src/reflection/core.py`.
- Assert the PAD delta direction and rough magnitude band per fixture, not exact
  numbers.
- Confirm the ephemeral affect instruction leads the next turn to color tone
  rather than announce mood.

This eval is the only check that the interesting behavior works; it stays manual
because it costs tokens and is non-deterministic.

## Completion Criteria

The bootstrap is complete when:

- All four new services run independently under systemd.
- Existing safety and hardware paths are unchanged.
- Voice remains functional with every new service stopped.
- Completed exchanges can persist and continue across voice sessions.
- Explicit remember and reflection can update a human-readable wiki.
- Current physical, presence, and affect state is available through one model
  snapshot.
- Affect and state reach the LLM through the ephemeral per-turn system message.
- Stable anonymous presence produces a transient event consumed by brain.
- Brain makes observable but non-acting greeting decisions.
- Dashboard and tests make each boundary inspectable.
- No implementation requires ROS2, while physical domain logic remains separable
  for the later migration.
