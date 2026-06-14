> THIS IS AN ARCHITECTURE IDEA, NOT A PLAN OR COMMITMENT.
>
> The names, process boundaries, storage choices, and message shapes below are
> provisional. The purpose of this note is to preserve the current model so it
> can be refined against real implementations.

# Robot state architecture

The ideas in this folder need several different kinds of plumbing. Treating all
of them as "robot state" would produce an everything-service. Treating each as a
separate service immediately would produce unnecessary distributed machinery.

The useful boundaries are conceptual first. Multiple roles may share one process
until their lifecycles or failure modes justify splitting them.

## The pieces

### Observations / telemetry

Current reports from hardware and perception producers:

- battery voltage and motor status
- range readings
- face detections
- voice activity and direction of arrival
- service health and freshness

Telemetry answers: **what are producers reporting now?**

It does not own history or decide what those reports mean. The current telemetry
hub is the pre-ROS2 transport and latest-value view for these observations.

### Events

Discrete things that happened:

- a conversation exchange was committed
- a motion command completed or failed
- battery state crossed into low
- someone arrived or left
- an identity was resolved

An event bus distributes these occurrences. It does not need to retain them.
Commands remain separate request/reply or action interfaces; an event describes
something that happened, not something another component must do.

### Current model

Domain processors turn observations and events into interpreted current state:

- self-state: battery condition, mobility, capabilities, recent physical events
- presence: who or what appears present, with timing and hysteresis
- affect: current mood and short-lived emotional state
- identity fusion: the current best identity attribution from available evidence

These processors own time-based interpretation and meaningful transitions. They
may initially run together in one `robot-model` process, but they are separate
domains rather than one generic interpretation framework.

The model answers: **what currently appears to be true?**

It publishes structured state and transition events. It does not compose prompts
or own durable memories.

### Historical evidence and durable knowledge

One SQLite database can hold two semantically different kinds of record:

- **journal:** immutable evidence, such as exact committed exchanges and notable
  robot events
- **knowledge:** revisable conclusions, such as preferences, person records,
  relationships, and learned routines

For example:

```text
Journal:   Arlen said, "I hate the vacuum."
Knowledge: person:arlen dislikes the vacuum; evidence: event 1842
```

The journal records what happened. Knowledge records what the robot currently
believes and may be corrected, merged, or forgotten.

A small `robot-memory` service may eventually own the database when several
processes need coordinated access. It can begin as a direct storage module while
there is only one writer.

### Reflection

Background interpreters consume historical evidence and propose changes:

```text
committed conversation events
    -> background LLM
    -> affect updates, memory proposals, relationship updates
```

Reflection does not own current state or durable storage. It submits explicit
updates to their owners. Physical transitions normally use deterministic domain
processors rather than an LLM.

### Context composition

The consumer of an LLM owns how structured information becomes model context.
For conversation, `robot-voice` gathers:

- its active conversation transcript
- current self, presence, and affect state from the model
- relevant durable facts from memory

It then renders those into the conversational model's prompt. The model and
memory systems should not publish prompt prose.

## Conversation ownership

- `robot-voice` owns the active, possibly unfinished transcript.
- Completed exchanges are emitted as events and written to the journal.
- Reflection interprets journaled exchanges asynchronously.
- Durable conclusions are written to knowledge storage.
- Current consequences such as affect changes are applied to the current model.

This keeps exact evidence, current conditions, and learned conclusions distinct.

## Behavior and commands

`robot-brain` is the likely behavior coordinator. It consumes model transitions
and decides whether to act:

- ignore an arrival
- greet someone
- perform a bounded gesture
- remain quiet because the robot is busy or suppressed

It sends commands through the existing owner of each capability. Motion safety
and execution remain in `robot-motion`; speech execution remains in
`robot-voice`. The event bus is not used as a command bus.

## Conceptual flow

```text
hardware / perception / voice
       | observations and completed occurrences
       v
telemetry transport + event bus
       |                         \
       v                          v
current-state processors       event journal
       |                          |
       |                          v
       |                       reflection
       |                          |
       v                          v
structured current model      durable knowledge
       |                          |
       +------------+-------------+
                    v
       behavior or context composition
                    |
                    v
             motion / voice commands
```

## Mapping the idea documents

| Idea | Likely owner |
| --- | --- |
| self-model | current model: self-state |
| presence | current model: presence |
| subconscious | reflection producing affect updates |
| personality state block | voice context composition |
| proactive behavior | robot-brain |
| embodied affect | robot-brain using model transitions |
| identity registry | durable knowledge |
| face matching | vision recognizer |
| voice matching | voice recognizer |
| multi-person conversation | robot-voice using presence and identity |
| memory | journal, reflection, and durable knowledge |
| self-created tools | durable routine definitions executed through existing commands |

## Deployment restraint

These boxes are not automatically services. Split a role into its own process
when it needs an independent lifecycle, failure boundary, hardware owner, or
concurrent access boundary. Until then, keep the implementation as plain modules
inside the smallest suitable process.

The architecture should preserve the distinctions without requiring a miniature
distributed platform on the Pi.

## ROS2 direction

ROS2 can replace the transport and execution side without requiring voice,
personality, reflection, or memory to become ROS2 nodes.

- Hardware observations become ROS2 topics.
- Physical commands become ROS2 topics, services, or actions as appropriate.
- Current-state processors that depend heavily on robot topics can become ROS2
  nodes.
- A small bridge exposes selected ROS2 state and commands to external Python
  processes through a stable local API.
- Voice and personality stay outside ROS2 and use that bridge plus the memory
  interface.

The detailed bridge boundary is still open and should be chosen from actual
message and command needs rather than mirroring every ROS2 topic externally.
