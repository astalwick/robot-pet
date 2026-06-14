# Robot State Architecture Bootstrap Decisions

This doc captures the fixed semantic decisions required before planning a
walking skeleton for observations, events, current interpreted state, history,
reflection, memory, context composition, and behavior coordination.

## Summary

The intended phase should establish real boundaries and at least one working
path through most of the systems described in
`docs/ideas/_robot_state_architecture.md`. It should create something useful to
iterate on without attempting to finish mood, identity, presence, memory, or
proactive behavior.

The future physical runtime will use ROS2, while voice, personality, reflection,
and durable cognitive storage are expected to remain ordinary Python processes.
This phase therefore needs contracts and domain logic that do not depend on the
current Unix-socket transport or require non-ROS applications to import ROS2.

## How Things Work Today

### Process and lifecycle boundaries

The robot currently runs as independent systemd services. Hardware and execution
ownership is already explicit:

- `robot-motion` exclusively owns the RoboClaw, applies range-sensor safety, and
  executes gamepad and voice motion requests.
- `robot-sensors` owns the ToF range hardware.
- `robot-camera` owns the camera and serves JPEG snapshots and MJPEG over HTTP.
- `robot-vision` consumes camera snapshots, detects faces, and publishes face
  boxes.
- `robot-voice` owns wake detection, active conversation sessions, STT, LLM
  turns, TTS, and conversational tools.
- `robot-brain` is currently only a sleeping loop; no behavior coordination has
  been implemented.

The repository's existing architecture treats service wrappers and IPC as
replaceable scaffolding. Hardware drivers and policy code are intended to remain
plain Python when ROS2 replaces the wrappers.

### Observations and telemetry

Services publish newline-delimited JSON `source_update` messages to
`robot-telemetry`. The hub retains only the latest update per source, adds source
freshness, combines selected fields into a single snapshot, and broadcasts that
snapshot to connected subscribers at a fixed rate.

Telemetry currently has no event semantics, replay, acknowledgement, durable
history, or per-subscriber filtering. A new subscriber receives the current
snapshot and then repeated full snapshots.

Despite being described in some documentation as dashboard scaffolding,
telemetry is already on operational paths:

- `robot-motion` subscribes to sensor snapshots for safety gating.
- `robot-battery` subscribes to battery/motion information for motor-rail power
  policy.
- dashboards and `robot-voice` also read telemetry.

Stopping or replacing the telemetry hub therefore affects more than
observability.

### Commands

Commands do not travel through telemetry. Existing capability owners expose
dedicated interfaces:

- gamepad drive commands use `motion-drive.sock`
- bounded voice motion intents use `motion-intent.sock`
- voice control commands use `voice-command.sock`
- camera snapshots use HTTP

The motion interfaces provide request-specific behavior such as acknowledgement,
timeouts, arbitration, and preemption. There is no generic command bus.

### Conversation state

`robot-voice` creates a `VoiceSession` only after a wake/session trigger. The
session owns an in-memory `ConversationHistory`, currently a bounded deque of
anonymous user/assistant exchange pairs. It is used to construct LLM input and
is discarded when the voice session ends.

Only completed, non-speculative turns are appended to conversation history.
There is no callback or transport for committed exchanges, no past-session
archive, and no speaker identity in the history model.

### Prompt and robot context

The conversational system prompt currently contains character prose followed by
the operational prompt. It is rebuilt when personality changes, but it has no
per-turn current-state or memory block.

The model can explicitly call `inspect_robot`. That tool fetches a telemetry
snapshot and returns a curated subset of fresh battery, drive, sensor, vision,
and Pi status. This information is not automatically supplied on each turn.

### Persistence and interpretation

There is no SQLite database or durable event journal. Persistent runtime settings
are separate JSON files under `/home/pi/.config/robot-pet/`, while personality
cards are Markdown files in the repository.

There is currently no component that:

- records completed conversations across sessions
- derives durable facts from conversations
- maintains interpreted self, presence, identity, or affect state
- emits semantic transition events
- performs background reflection
- retrieves relevant memories for a turn

### ROS2 direction already present

The current physical interfaces were designed with migration in mind. Motion
commands are already Twist-shaped, hardware drivers are transport-independent,
and the architecture expects current service entrypoints to be replaced by ROS2
nodes.

No bridge contract currently exists for non-ROS applications such as voice to
read selected ROS2 state, receive events, or invoke ROS2 services/actions.

## Decision Areas Covered

This session resolves choices covering:

- the first process/deployment boundaries
- whether to introduce a distinct event transport now
- ownership and write access for the journal and durable knowledge
- current-model domains included in the walking skeleton
- the first end-to-end workflows
- the external API boundary intended to survive the ROS2 transition
- startup, outage, retry, and replay behavior
- minimum observability and operator correction surfaces
- rollout alongside the existing telemetry-dependent safety paths

## Initial Process Boundaries

### Question

Should the bootstrap create separate systemd processes for events, current model,
memory, and reflection, or initially combine some of those responsibilities in a
single process?

### Decision

Create four separate systemd processes:

- `robot-events`
- `robot-model`
- `robot-memory`
- `robot-reflection`

These responsibilities have meaningfully different lifecycles and failure modes.
In particular, reflection may be slow or unavailable because it calls a networked
LLM; memory must coordinate durable database access; and event distribution plus
the current model should remain continuously available independently of either.

`robot-events` stays separate rather than folding into `robot-telemetry` or
`robot-model`. Telemetry is on the motion-safety path and shares one asyncio loop,
so a wedged event subscriber must not be able to backpressure the safety broadcast.
`robot-model` is the process tuned and restarted most often during this work, so the
shared bus must not depend on its uptime. A tiny, always-up fanout process whose
code rarely changes is the point, not an overhead to eliminate.

Each service entrypoint must remain a thin lifecycle and transport wrapper around
plain Python domain modules. The process split establishes ownership and failure
isolation now without coupling the underlying logic to systemd or ROS2.

## Event Delivery Semantics

### Question

Should `robot-events` retain and replay events, or provide transient fanout only?

### Decision

`robot-events` provides transient fanout only. It distributes live occurrences
to interested subscribers but does not retain history, track durable consumer
offsets, or guarantee replay after a subscriber outage.

Anything that must survive failure or be processed eventually must first be
written to the journal owned by `robot-memory`. Background workers such as
reflection read durable journal rows using their own cursor rather than relying
on event-bus delivery. This keeps persistence in one system and avoids building a
second database or replay broker inside the event transport.

## SQLite Ownership

### Question

Should durable journal, knowledge, and current-model checkpoints use separate
databases or one shared SQLite database, and which processes may write it?

### Decision

Use one shared SQLite database, `robot.db`, with WAL mode and short transactions.
Multiple services may connect directly, but each service writes only the tables
it owns:

- `robot-memory` owns conversation-journal tables.
- `robot-model` owns current-model checkpoint tables, including persisted affect.
- `robot-reflection` owns its reflection-progress table.

Database schema creation and migrations are shared code rather than owned by one
runtime service. Cross-domain writes still go through the owning service's API;
for example, voice submits journal records to `robot-memory` rather than inserting
rows itself. This keeps semantic ownership explicit without creating multiple
SQLite files or routing model checkpoints through the memory service.

## Conversation Journal Delivery

### Question

Must `robot-voice` wait for or guarantee durable recording of each completed
exchange?

### Decision

No. Completed exchanges are submitted to `robot-memory` on a best-effort,
fire-and-forget basis after the turn. Voice does not block, wait for an
acknowledgement, maintain an outbox, or retry failed submissions.

Occasional loss of conversation history during a service outage or crash is
acceptable. Keeping the conversational path simple and independent of memory
availability matters more than guaranteed archival delivery.

## Initial Current-Model Scope

### Question

Should the first `robot-model` implementation cover only interpreted physical
telemetry, or also establish presence and affect domains?

### Decision

The bootstrap includes three intentionally small current-state domains:

- **physical self-state:** battery condition, mobility/capability, and a recent
  motion outcome or blockage
- **anonymous presence:** nobody/someone present, without identity recognition
- **global affect:** persisted three-axis PAD state plus an optional short cause

The purpose is to establish that `robot-model` can combine physical observations,
time-based social interpretation, and externally supplied affect updates behind
one structured current-state interface. It does not attempt complete self-model,
presence fusion, identity, or psychological modeling in this phase.

## Initial Presence Input

### Question

Which observation should drive anonymous presence in the bootstrap?

### Decision

Anonymous presence initially uses only face detections already published by
`robot-vision`. `robot-model` applies simple arrival and departure hysteresis to
derive whether someone appears present and emits transitions when that condition
changes.

Direction of arrival, voice activity, face tracking, identity matching, and
multi-sensor fusion are deferred. Current DoA state is tied to the voice
lifecycle and should not become a dependency of always-on presence in this phase.

## Telemetry Rollout

### Question

Should the bootstrap replace existing telemetry flows, or operate alongside the
current hub and consumers?

### Decision

The new systems are additive. `robot-model` subscribes to the existing telemetry
snapshot stream for current physical and vision observations. `robot-events`
handles discrete semantic events separately.

Existing operational telemetry paths remain unchanged, especially the sensor
data consumed by `robot-motion` for safety and the state consumed by
`robot-battery` for motor-rail policy. This phase does not reroute those paths
through `robot-model` or the event bus. Their transport can move directly to ROS2
later without coupling safety behavior to the cognitive bootstrap.

## Bootstrap IPC

### Question

Which transport should the new services use before ROS2 is introduced?

### Decision

Use newline-delimited JSON over local Unix sockets, matching the project's
existing service communication style. Message contracts should describe semantic
operations and records rather than mirror internal classes or raw telemetry
layout. Expected examples include current-state reads, live event subscription,
conversation journal submission, memory queries, and affect updates.

The domain modules must not depend on the socket transport. During ROS2 migration,
physical observations and commands can move to ROS2 while a bridge continues to
offer stable semantic interfaces to non-ROS Python applications such as voice,
reflection, and memory clients.

The two low-level socket primitives already in `src/telemetry/socket_client.py` —
a reconnecting line-subscribe and a fire-and-forget line-publish — are factored once
into a shared module and reused by events, the model stream, and memory, rather than
copied into a parallel `events/socket_client.py`. By the time those clients exist the
same boilerplate has been written well past three times. Each service still defines
its own message contracts on top; this is shared transport plumbing, not a generic
message framework.

## Reflection Timing And Outputs

### Question

When should `robot-reflection` review conversation history, and must each review
produce state or memory changes?

### Decision

Reflection processes batches of unreviewed conversation exchanges at the end of
a voice session or after a conversation idle gap. It does not call the LLM once
per turn.

Each reflection pass may produce neither, either, or both of:

- an affect update for `robot-model`
- one or more proposed durable facts for `robot-memory`

No mutation is required. Ordinary conversation should commonly produce no
durable fact and little or no affect change. The reflection prompt and output
schema must make omission explicit rather than forcing a conclusion from every
batch.

## Reflection Progress

### Question

How should reflection avoid repeatedly processing the same conversation
exchanges across idle cycles and service restarts?

### Decision

Store one durable reflection cursor in the shared SQLite database. After a batch
has been processed, `robot-reflection` advances the cursor to the highest journal
record included in that batch.

Reflection reads later conversation records on the next pass. The bootstrap does
not add queue semantics or reviewed/status columns to every journal row. If a
reflection call fails before the cursor advances, that batch may be attempted
again later.

## Initial Journal Scope

### Question

Should semantic transitions produced by `robot-model` be persisted in the event
journal during the bootstrap?

### Decision

No. Initial model transitions such as presence, battery-condition, or mobility
changes are published live through `robot-events` only. The first journal stores
completed conversation exchanges and the session information needed to batch
them for reflection.

Durable physical-event history can be added when a concrete memory, reflection,
or debugging workflow needs it. The bootstrap does not archive events merely
because they exist.

## Initial Brain Behavior

### Question

Should `robot-brain` perform a real action in the bootstrap?

### Decision

`robot-brain` subscribes to anonymous-presence transitions and applies a minimal
greeting policy, but it does not speak or move. When stable presence changes from
nobody to someone, it logs or publishes that it would greet. This proves event
consumption and policy placement without introducing surprise autonomous
behavior.

The trigger must use the hysteretic presence state from `robot-model`, never raw
per-frame face detections. Face detection currently appears and disappears too
frequently to represent arrival or departure directly.

Turning a would-greet decision into actual speech is explicitly out of scope for
this architecture phase, and not a fast-follow. The brain logs or publishes the
decision and nothing more. Autonomous speech requires its own separate, later
decision; the bootstrap adds no path from a brain decision to voice or motion.

## Presence Hysteresis

### Question

How long must face detection remain present or absent before anonymous presence
changes state?

### Decision

Anonymous presence becomes present after a face has been detected continuously
for 2 seconds. It becomes absent after no face has been detected for 10 seconds.

The initial Haar detector publishes face boxes but no useful confidence score, so
the bootstrap uses duration rather than confidence. These timings are initial
policy values intended to suppress normal detector flapping and can be tuned from
observed behavior.

## Initial Greeting Policy

### Question

What conditions should the observe-only `robot-brain` policy use to decide that
an arrival would merit a greeting?

### Decision

`robot-brain` records a would-greet decision only when all of these conditions
hold:

- `robot-model` emits a stable anonymous arrival transition.
- No conversation exchange has been committed within the previous 30 seconds.
- The brain has not made another would-greet decision within the previous 5
  minutes.

Presence interpretation remains in `robot-model`; combining presence with recent
interaction timing and cooldowns is orchestration policy owned by `robot-brain`.
The bootstrap logs the decision and suppression reason but does not invoke voice
or motion.

The brain keeps its last-conversation and last-would-greet timestamps in memory
only. Restarting `robot-brain` resets those cooldowns. The observe-only bootstrap
does not persist orchestration policy state.

## Per-Turn Context Composition

### Question

Should `robot-voice` automatically include current model state and relevant
durable memory on each conversational turn?

### Decision

Yes. Before each committed LLM turn, `robot-voice` composes context from:

- its existing active conversation history
- the latest locally cached `robot-model` snapshot
- the current global `self` and `world` knowledge from `robot-memory`

Both external inputs are best-effort and use short time bounds. If model state or
memory is unavailable, voice omits that portion and continues the turn. Neither
service becomes a prerequisite for conversation.

Voice owns the final rendering into prompt text. `robot-model` and
`robot-memory` return structured data rather than LLM-specific prose.

## LLM Input Ordering

### Question

Where should stable instructions, durable knowledge, recent conversation, and
current robot state appear in each conversational model request?

### Decision

The initial system prompt contains stable content in this order:

```text
operational instructions
character
durable knowledge
```

Recent conversation remains ordinary user and assistant messages in chronological
message history. It is not folded into the system prompt.

For each new turn, voice builds the model input as:

```text
stable system prompt
prior user/assistant messages
ephemeral system message containing current robot state and affect
newest user message
```

The ephemeral system message is rebuilt from the latest cached `robot-model`
snapshot for every turn. It is never appended to active conversation history or
written to the conversation journal. This keeps changing embodied context close
to the user message it qualifies without polluting transcript continuity.

The OpenAI Responses API accepts `system` messages in the input sequence, and
system/developer instructions take precedence over user messages. The bootstrap
uses `system` for this state item unless model-specific testing identifies a
reason to use `developer` instead.

## Ephemeral State Semantics

### Question

How should the conversational model treat physical state and affect from the
ephemeral per-turn system message?

### Decision

Physical self-state is factual grounding. The model may phrase those facts in the
active character's voice, but it must not contradict them or claim unavailable
capabilities.

Affect is expression guidance. It should color tone, word choice, and restraint
without causing Bloop to announce or explain its internal mood by default. The
model may name the feeling when the user directly asks, when the cause is
conversationally relevant, or when withholding it would make the response
confusing.

The ephemeral message must state this distinction directly rather than presenting
physical facts and affect labels as an undifferentiated status dump.

## Durable Knowledge Storage

### Question

Should durable learned facts be stored as rows in SQLite and retrieved through
keyword matching, or use a human-readable filesystem representation?

### Decision

Durable knowledge uses a filesystem wiki owned by `robot-memory`, while SQLite
remains the operational store for the conversation journal, reflection cursor,
and current-model checkpoints.

The knowledge tree is organized into readable Markdown files, along lines such
as:

```text
memory/
  self/facts.md
  world/facts.md
  people/arlen/facts.md
```

The exact taxonomy may begin smaller because identity is not part of the
bootstrap. `robot-memory` owns reading and writing these files so callers do not
depend on paths or Markdown layout.

For the initial small fact set, voice may receive the relevant global fact files
directly rather than relying on weak keyword retrieval. Search and ranking can be
introduced when the amount of knowledge makes full loading inappropriate.

## Initial Knowledge Recall

### Question

Which durable wiki knowledge should `robot-voice` receive for each turn during
the bootstrap?

### Decision

Load all global `self` and `world` knowledge for each turn. The bootstrap fact
set is expected to remain small enough that explicit retrieval and ranking would
add machinery without improving behavior.

Person-specific knowledge is deferred until the robot has a real identity and
speaker-attribution path. Voice should not guess a person from keywords or load
all future relationship files indiscriminately. Retrieval can be introduced when
the global knowledge set becomes large enough to create a real prompt-budget
problem.

Global durable knowledge is refreshed from `robot-memory` for each committed
turn, not only when a voice session starts. A successful explicit remember or
reflection update can therefore affect the next turn in the same session. The
read remains best-effort and may be cached internally as an implementation detail
as long as updates become visible promptly.

## Conversation Continuity Across Sessions

### Question

Should a new voice session begin with no transcript context, or continue recent
conversation history from earlier sessions?

### Decision

Conversation context continues across voice-session boundaries. Ending a voice
session stops active listening and cloud conversation work; it does not imply
that Bloop forgets the recent discussion.

At the beginning of a new session, `robot-voice` loads recent journaled exchanges
from `robot-memory` and combines them with exchanges committed during the active
session. Historical exchanges include wall-clock timestamps or explicit elapsed
time markers so the conversational model can distinguish a brief pause from a
substantial gap and does not assume the prior topic is still active.

This transcript continuity is separate from durable wiki knowledge. The journal
preserves recent conversational sequence; the wiki contains consolidated facts
that remain useful after detailed transcript context ages out.

## Conversation History Window

### Question

How much journaled conversation history should a new voice session load?

### Decision

Load committed exchanges from the previous 2 hours, capped at the newest 40
exchanges. Preserve each exchange's timestamp or render elapsed-time markers so
the model can recognize pauses and session boundaries within that window.

The time window prevents old discussions from silently becoming the active topic,
while the exchange cap places a hard bound on prompt growth during a long or
frequently interrupted conversation.

Both limits are runtime configuration rather than hard-coded policy. Defaults are
2 hours and 40 exchanges.

## Reflection Independence From Prompt History

### Question

Should reflection timing or eligibility depend on the conversation-history window
that voice loads into the LLM?

### Decision

No. Reflection runs only at voice-session end or after its configured idle gap,
processing journal records after its own durable cursor. The two-hour/forty-
exchange prompt-history window affects only what `robot-voice` supplies to the
conversational model.

## Tentative ROS2 Model Split

### Question

When ROS2 is introduced, should the complete current model remain outside ROS2 or
should physical domains move into the ROS graph?

### Decision

Tentative direction: physical self-state and presence are likely to become ROS2
nodes because they consume robot observations and produce embodied state/events,
while affect is likely to remain in the non-ROS cognitive application layer. A
bridge would expose the selected combined state needed by voice and other
non-ROS consumers.

This is not a final ROS2 deployment decision. It must be revisited when the real
ROS2 graph, message types, process placement, and bridge needs are known. The
bootstrap should nevertheless keep physical self-state, presence, and affect as
separate domain modules so either placement remains straightforward.

## Bootstrap Observability

### Question

What operator visibility should the bootstrap add for the new architecture?

### Decision

Add read-only web-dashboard visibility for:

- the latest structured `robot-model` snapshot, including raw and derived affect
- a bounded recent-event view from the live `robot-events` stream
- recent conversation journal entries and reflection cursor/status
- the current filesystem-wiki knowledge documents
- the latest would-greet decision and suppression reason from `robot-brain`

The bootstrap does not add dashboard editing, approval, replay, or manual event
injection. Knowledge remains directly editable on disk, and existing configuration
editing behavior remains unchanged.

## Conversation Journal Shape

### Question

Should conversation history be stored as one transcript document per voice
session or as individual committed exchanges?

### Decision

Store one journal row per committed exchange. Each row includes at least a stable
row ID, voice `session_id`, user text, assistant text, and wall-clock timestamps.

Session IDs preserve grouping without making session boundaries the storage unit.
Exchange-level rows support reflection cursors, the configurable cross-session
history window, and future speaker attribution without rewriting transcript
blobs. Speculative, cancelled, or uncommitted turns are not journaled.

## Journaled Exchange Content

### Question

Should committed exchange rows also retain LLM tool calls and their results?

### Decision

No. The bootstrap journal stores the committed user text, final assistant text,
session grouping, and timestamps only. It does not store tool-call requests,
arguments, intermediate model responses, or tool outputs.

Tool evidence can be added later if a concrete memory, audit, or debugging
workflow requires it. The initial journal is conversation history rather than a
complete execution trace.

## Journal Retention

### Question

Should the bootstrap automatically expire or delete old conversation journal
rows?

### Decision

No automatic retention limit or deletion policy is added in the bootstrap.
Committed conversation rows remain in SQLite indefinitely.

The expected volume is small, and there is not yet evidence for an appropriate
age, size, or privacy policy. Retention can be revisited when actual storage
growth or privacy requirements make the tradeoff concrete. Manual database
maintenance remains possible in the meantime.

## Voice Service Dependencies

### Question

Should `robot-voice` require the new model, memory, events, or reflection services
to be running before it can start and converse?

### Decision

No. Voice has no hard runtime or systemd dependency on the new cognitive
services. Units may use soft startup ordering such as `Wants=` and `After=` where
helpful, but voice must start and operate when any or all of them are unavailable.

Current-state context, knowledge loading, conversation journaling, and lifecycle
events are all best-effort integrations. Their failure may reduce continuity or
personality behavior, but it must not disable wake, STT, LLM response, TTS, or the
existing tools.

## Event Dependency And Fallbacks

### Question

Should services continue event-driven work through polling or alternate paths
when `robot-events` is unavailable?

### Decision

No. The bootstrap does not build redundant delivery paths. Features that depend
on live events stop functioning while `robot-events` is unavailable and resume
when it returns.

In particular, consumers must not poll the journal, telemetry, or another service
as a fallback substitute for missed event delivery. Services may continue work
that does not depend on events, such as serving existing memory or current-state
queries, but event-triggered reflection, orchestration, and transition handling
remain inactive until the event stream is restored.

## Event Stream Reconnection

### Question

Should event subscribers reconnect automatically after `robot-events` restarts?

### Decision

Yes. Event client adapters automatically reconnect to the same primary event
stream after disconnection, using a simple fixed reconnect interval consistent
with the current telemetry subscriber.

This does not add replay or recovery. Events emitted while the bus or subscriber
was disconnected are lost. Reconnection only restores future live delivery.

## Event Publication Semantics

### Question

Should `robot-events` acknowledge publication or require producers to wait and
retry?

### Decision

No. Event publication is fire-and-forget. Producers send one event and continue
without waiting for an acknowledgement or retrying failed delivery.

Publish failures may be logged or counted for observability. Losing an event
during a bus outage is consistent with the transient event semantics. Workflows
requiring durable evidence must write it to the appropriate store rather than
relying on the live event stream.

Reflection cadence, progress, and batching are independent of conversational
context loading. A transcript may still be available to voice after reflection
has processed it, and aging out of voice context does not trigger reflection.

## Reflection Trigger Events

### Question

How should `robot-reflection` know that a conversation has ended or become idle?

### Decision

`robot-voice` publishes conversation lifecycle events through `robot-events`,
including activity after a committed exchange and an explicit session-ended
event. `robot-reflection` owns a configurable idle timer that resets on committed
conversation activity and runs a reflection pass when it expires. The default
idle gap is 60 seconds.

An explicit session-ended event schedules reflection after a fixed 10-second
delay, allowing the best-effort conversation journal submission to land without
adding acknowledgement or ordering choreography. Reflection does not poll the
journal to infer conversational activity; the journal remains the durable input
it reads once a lifecycle event or idle timer says a batch is ready.

## Reflection Startup Catch-Up

### Question

If `robot-reflection` starts with conversation journal rows after its durable
cursor, should it process them without having received the original transient
session-ended or idle event?

### Decision

Yes. On startup, reflection checks for conversation rows after its cursor and
runs one catch-up pass when any exist. This prevents a temporary reflection
outage from permanently leaving journaled conversations uninterpreted.

A startup backlog is treated as abnormal and logged as an error because it means
reflection missed live processing while stopped or disconnected. The service
still continues by processing the backlog; the error is diagnostic rather than
fatal.

This startup check is retained only because reflection already needs its cursor
and next journal rows to perform normal work. It is not a fallback loop:
reflection does not poll the journal while `robot-events` is unavailable and does
not attempt to reconstruct missed live triggers during normal operation.

## Reflection Failure Behavior

### Question

What happens to reflection progress when an LLM call, output parse, affect update,
or memory write fails?

### Decision

The reflection cursor remains unchanged when the pass does not complete. The
batch becomes eligible again on the next session-ended event, idle trigger, or
service startup.

The bootstrap does not add an internal retry loop, exponential backoff, or retry
queue. Reflection logs the failure and waits for the next normal trigger. A later
pass may therefore repeat work that partially succeeded; affect and memory update
interfaces should tolerate duplicate submissions where practical, but no broader
exactly-once machinery is required.

## Current-Model Persistence

### Question

Which `robot-model` domains should survive a service or robot restart?

### Decision

Persist affect only. The PAD values, optional cause, and last-update time are
checkpointed in the model-owned tables of the shared SQLite database.

Physical self-state is rebuilt from fresh telemetry after startup. Anonymous
presence begins unknown or absent and is re-established through live face
detections and the normal hysteresis thresholds. Presence is not restored from a
previous run because the person may have left while the model was stopped.

## Physical-State Authority

### Question

May `robot-model` introduce its own battery-voltage, obstacle-distance, or motion-
safety thresholds when deriving physical self-state?

### Decision

No. Battery, sensor, motion, and safety thresholds remain owned by the services
that directly understand and enforce those physical domains. `robot-model`
consumes their published semantic fields, such as battery status, drive state,
safety blocked status, stop reason, and capability readiness.

The model may combine those reported meanings into a conversational/current-state
view, but it must not independently reinterpret raw voltage or distance readings
into competing safety or health conclusions.

## Initial Physical Self-State Fields

### Question

Which physical facts should `robot-model` expose in its first self-state
projection?

### Decision

Expose only:

- semantic battery status
- motion availability and current motion state
- whether safety is blocking motion and the reported reason
- camera and vision availability
- the most recent bounded motion outcome when available

Raw pack voltage, individual sensor distances, CPU health, memory usage, disk
usage, and similar diagnostics remain in telemetry and dashboard tooling. They are
not included in the current model or conversational context unless a later feature
has a concrete need for them.

## Existing inspect_robot Tool

### Question

The conversational model can already call `inspect_robot` for a curated telemetry
subset. Does it remain once per-turn ephemeral state is pushed automatically?

### Decision

Keep it, but re-scope it to a detailed diagnostics view fetched on request. The
every-turn ephemeral block carries the semantic summary (battery status, presence,
mobility, affect). `inspect_robot` owns the raw detail the ephemeral block
deliberately omits: pack voltage, individual sensor distances, SoC temperature,
memory, disk, uptime, and throttling. It stops returning the summary fields now
supplied every turn so the two paths cannot drift or contradict.

The tool description must be updated to present it clearly as an on-demand detailed
diagnostics view, not an overlapping copy of the always-on state. Push = semantic
state every turn; pull = raw diagnostics when the user actually asks.

## Runtime Data Location

### Question

Where should the shared SQLite database and filesystem knowledge wiki live on the
robot?

### Decision

Store generated persistent robot data under:

```text
/home/pi/.local/share/robot-pet/
```

The default database path is
`/home/pi/.local/share/robot-pet/robot.db`, and the wiki lives beneath a
`memory/` directory in the same root. These are runtime-generated data rather
than operator configuration or repository assets, so they do not belong under
`/home/pi/.config/robot-pet/` or in the checked-in source tree.

All paths remain injectable for tests, which use temporary directories and
databases.

## Memory Service Interface

### Question

Should journal and knowledge operations use separate transports or one
`robot-memory` request interface?

### Decision

`robot-memory` exposes one newline-delimited JSON request/reply Unix socket. An
operation name and structured payload distinguish journal submission, recent
history reads, global wiki reads, candidate-fact writes, and debug/status reads.

The service handles one request and returns one response per connection unless a
later usage pattern justifies persistent clients. Callers that intentionally use
best-effort fire-and-forget semantics, such as voice journal submission, may send
the request and close without waiting for the response. No separate socket is
created for each storage capability.

## Verification Scope

### Question

What automated verification is required for the bootstrap architecture?

### Decision

Add focused unit tests for the transport-independent event, model, memory,
reflection, context-composition, and brain-policy modules. Tests cover state
transitions, presence hysteresis, PAD persistence/decay, journal/history queries,
wiki merging, reflection cursor behavior, and greeting suppression.

Also add one process-level integration test covering the principal cognitive
path:

```text
committed exchange
  -> journal
  -> reflection produces optional affect/fact updates
  -> model and wiki store them
  -> voice context reads them
```

Hardware access and hosted LLM calls are mocked in automated tests. Tests run
with the repository-standard `python3 -m unittest ...` commands.

Add one manually-run scenario eval, separate from CI and not using fakes, for the
behaviors mocked-LLM unit tests cannot reach: that reflection moves PAD deltas in
the expected direction and rough magnitude on contrasting transcripts (a sneer vs. a
warm compliment), and that the ephemeral affect instruction keeps the conversational
model from announcing its mood by default. These assert sign and magnitude bands
against the real `gpt-5.4-mini`, since the value of affect is exactly the part fakes
cannot verify. The Phase 7 dashboard PAD readout is the live counterpart and must
ship together with affect, not after it.

## Runtime LLM Use

### Question

Should reflection and semantic knowledge merging use the real OpenAI API in the
bootstrap runtime, or remain disabled/mock-only until later?

### Decision

Use the real OpenAI API by default, with the existing `OPENAI_API_KEY` environment
credential. `robot-reflection` uses it for batched conversation interpretation.
`robot-memory` does not call an LLM in the bootstrap; it appends candidate facts
with provenance (see Knowledge Merge Behavior).

When credentials are absent, reflection's LLM-backed interpretation is disabled and
logged rather than preventing the rest of the service from starting. Memory,
journaling, and current-state reads continue to work without credentials. Automated
tests continue to use deterministic fakes and do not call hosted models.

## Background LLM Model

### Question

Which model should the initial reflection and knowledge-merge calls use?

### Decision

Default reflection to `gpt-5.4-mini`, matching the model currently used by the
conversational voice path. Reflection has its own configuration field rather than
sharing the voice model constant, so it can change independently later as its cost,
latency, and accuracy needs become clear.

Memory has no model in the bootstrap because candidate facts are appended rather
than merged. A future consolidation pass can introduce its own model field then.

## Affect Decay Across Downtime

### Question

Should persisted affect remain unchanged while `robot-model` or the robot is
stopped?

### Decision

No. On startup, `robot-model` applies affect decay using the wall-clock time
elapsed since the saved update, then continues normal decay while running. Each
PAD axis initially decays toward its neutral midpoint.

Decay rates are runtime configuration and may differ by axis. Their concrete
defaults can be selected and tuned during implementation; this decision fixes
the elapsed-time behavior rather than a particular psychological model.

## Affect Consumer View

### Question

Should prompt consumers interpret raw PAD values themselves, or should
`robot-model` expose a derived affect description?

### Decision

`robot-model` deterministically derives structured affect labels and intensity
from the PAD values and exposes those labels together with the optional cause.
`robot-voice` uses this derived view when composing prompt context rather than
inventing its own numeric thresholds or emotion mapping.

Raw PAD values remain present in the model snapshot for debugging and future
embodied-expression consumers. The derived description is structured data, not
prewritten prompt prose; voice still controls how it is expressed to the
conversational model.

## Explicit Remember Tool

### Question

How should a user explicitly tell the robot to retain a durable fact?

### Decision

Add a `remember` tool to `robot-voice`. When the user makes an explicit request
such as "remember that you are in Longueuil," the conversational model calls the
tool with a concise fact and an initial category. The tool submits the fact to
`robot-memory`, which updates the filesystem wiki it owns.

This explicit write path is separate from background reflection. Explicit user
requests can be stored immediately; reflection may independently propose facts
from ordinary conversation after a session or idle gap. The tool call is
best-effort and does not make the spoken conversation depend on storage
availability.

The tool is fire-and-forget. Voice does not wait for `robot-memory` to confirm
the write before responding, and it does not expose storage acknowledgement to
the user. The conversational response should remain immediate and natural, such
as "okay, sure," rather than pausing to report database success.

An occasional failed or forgotten memory is acceptable for this robot's intended
character and is preferable to making ordinary conversation feel like operating
a computer. Failures may be logged for debugging but do not alter the spoken
response.

## Remember Tool Policy

### Question

May the live conversational model call `remember` whenever it considers
something important, or only when the user explicitly asks it to remember?

### Decision

The live `remember` tool is used only for explicit user requests to retain a
fact. The model must not invoke it merely because an ordinary conversational
detail appears useful or memorable.

Facts inferred from normal conversation belong to the asynchronous reflection
path, where a batch can produce no memory at all. This keeps the live tool
predictable and limits wiki clutter while the memory format and consolidation
behavior are still immature.

Explicit `remember` facts use the same `robot-memory` append path as
reflection-derived facts. The provenance distinguishes an explicit user
instruction from an inferred reflection result; both are appended to the target
document with that provenance, and any deduplication or contradiction handling is
deferred to the later consolidation pass.

## Reflection Memory Writes

### Question

Should facts proposed by background reflection be written automatically or wait
for operator approval?

### Decision

`robot-reflection` may submit durable facts directly to `robot-memory`, which
writes them to the filesystem wiki without an approval step. Each stored fact
must retain provenance linking it to the journal records or reflection batch from
which it was derived.

The wiki remains human-readable and editable so incorrect conclusions can be
corrected or removed. The bootstrap does not add an approval queue or dashboard
workflow before autonomous memory is exercised.

## Knowledge Merge Behavior

### Question

How should `robot-memory` handle a new fact that duplicates or contradicts
existing knowledge?

### Decision

`robot-memory` appends each candidate fact to the target Markdown file as a new
provenance-tagged entry. It does not call an LLM to merge, rewrite, or consolidate
the file in the bootstrap. Provenance records the timestamp, whether the candidate
came from an explicit `remember` or from reflection, and any evidence journal row
IDs.

This was deliberately downgraded from an earlier LLM merge-in-place decision.
Rewriting an entire durable memory file on every write is the one place the
bootstrap would trade away minimalism, and it is exactly where a valid-looking
model response can silently drop an existing fact while still parsing. Blind append
with provenance cannot lose prior facts, is trivially testable, and keeps
`robot-memory` free of any LLM dependency. The wiki stays human-readable and
hand-editable, so duplication and contradiction are tolerated until a concrete
consolidation need appears.

Consolidation is deferred to a later manual or offline pass, which can be an LLM
step or deterministic rules without changing the service boundary. The append is
written through a temporary file and atomic replace so a crash cannot corrupt the
existing document.

## Current-Model Distribution

### Question

Should interpreted current state be added to the existing telemetry snapshot, or
served through a dedicated `robot-model` interface?

### Decision

`robot-model` exposes a dedicated Unix-socket state stream. A subscriber receives
the current structured model snapshot on connection and then receives updates as
the model changes. Consumers such as `robot-voice` and `robot-brain` maintain a
local cache from this stream.

`robot-model` publishes only its operational health and compact debug status to
the existing telemetry hub for dashboards. The full cognitive/current model does
not become another field embedded in the legacy telemetry snapshot. This keeps
raw observation transport distinct from interpreted model distribution and gives
the future ROS2 bridge a clear external state contract.

`robot-model` uses a separate small request/reply Unix socket for state-changing
requests such as `apply_affect_delta`. The snapshot subscription remains a
one-way state stream. This is a local transport detail, not a generalized command
framework, and may be adjusted during implementation if a simpler equivalent
fits the code better.

## Event Stream And Envelope

### Question

Should subscribers filter event types at subscription time, and what common shape
should all events share?

### Decision

`robot-events` provides one shared live stream. Every subscriber receives every
event and ignores types it does not consume. The expected bootstrap volume is too
small to justify server-side subscription filters.

Every event uses a common envelope containing at least:

```json
{
  "type": "presence_changed",
  "time": 1781395200.0,
  "source": "robot-model",
  "data": {}
}
```

`type` names the occurrence, `time` is wall-clock event time, `source` identifies
the producing service, and `data` contains event-specific structured fields.
Additional correlation or schema-version fields should be added only when a real
consumer requires them.

## Event Socket Shape

### Question

What local socket topology should `robot-events` expose?

### Decision

Use two Unix sockets, matching the existing telemetry transport pattern:

- one publisher socket accepting newline-delimited event envelopes
- one subscriber socket streaming each live event to connected subscribers

Unlike telemetry, the subscriber connection does not receive a synthesized
current snapshot on connect because events are transient occurrences rather than
latest state. Producers make short fire-and-forget connections; subscribers keep
a streaming connection and automatically reconnect after interruption.

## ROS2 Bridge Timing

### Question

Should the bootstrap create a `robot-api` bridge process before ROS2 exists?

### Decision

No. Before ROS2, a bridge would only proxy the local Unix-socket services without
crossing a genuine runtime boundary. Voice, brain, reflection, model, and memory
may use direct semantic socket clients during the bootstrap.

The client-facing contracts and domain logic must remain transport-independent so
a bridge can be added when physical observations and commands move into ROS2.
That later bridge will translate only the selected state, events, and commands
needed by non-ROS applications rather than preserving the current internal socket
topology wholesale.

## Affect Schema

### Question

Which affect dimensions should the bootstrap establish?

### Decision

Use the three-axis PAD model already proposed in `docs/ideas/inner-life-meta.md`:

- **pleasure / valence:** unpleasant to pleasant
- **arousal / energy:** subdued to activated
- **dominance / control:** powerless or constrained to capable or assertive

Each axis is stored numerically with a neutral midpoint, together with an
optional short cause supplied by reflection. The exact numeric range and decay
constants may be chosen during implementation, but the three semantic axes are
part of the contract from the start.

The bootstrap does not need an active producer for every axis. Dominance may
remain neutral until physical self-state or reflection has a concrete reason to
change it. Establishing it now prevents affect consumers from being designed
around an intentionally incomplete one-dimensional mood model.

## Affect Update Sources

### Question

Which bootstrap components may update each PAD axis?

### Decision

`robot-reflection` may propose bounded changes to pleasure, arousal, and dominance
based on a batch of conversation exchanges. It may omit any or all axes when no
change is warranted.

`robot-model` may also derive a bodily arousal contribution from current physical
state such as low battery or recent activity. The model combines that bodily
contribution with the slower reflection-derived affect state.

Physical dominance/control updates are deferred until concrete stuck/capable
behavior exists. The dominance axis remains present and neutral unless reflection
has a reason to change it. The bootstrap should not invent physical dominance
rules merely to exercise the field.

## Reflection Affect Output

### Question

Should reflection replace current affect with absolute PAD values or propose
changes relative to the existing state?

### Decision

Reflection returns optional bounded deltas for pleasure, arousal, and dominance,
plus an optional short cause. It does not choose absolute affect state.

`robot-model` owns applying the deltas to current reflection-derived affect,
clamping values, combining the bodily arousal contribution, persisting the
result, deriving consumer labels, and applying decay. This keeps the LLM's role
limited to interpreting how a conversation should shift affect rather than
controlling state mechanics.

## Affect Identity Scope

### Question

Should affect state be global to Bloop or independently maintained for each
selectable personality card?

### Decision

Maintain one robot-global affect state. Switching the active personality card or
voice does not reset mood or select a separate emotional history.

Personality cards may influence how the same affect state is expressed in prompt
context, speech, or later embodied behavior, but card-specific affect baselines,
gains, and decay are deferred. This treats the selectable cards as presentations
of one continuing robot rather than separate persistent identities.
