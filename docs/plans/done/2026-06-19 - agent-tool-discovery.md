# Agent Tool Discovery Plan

> **SUPERSEDED — WON'T DO (2026-06-20).** This plan's core goal (keep full tool
> schemas out of every model call as the tool set grows) is now covered by native
> OpenAI tool calls — see `docs/plans/2026-06-20 - voice-agent-native-tool-calls.md`
> — plus `tool_search` for on-demand schema discovery when the list actually grows
> large. At the robot's current scale (~7 tools) schema bloat is a non-problem, so
> building a markdown tool registry, a `call_tool` executor, and reimplemented Unix
> read tools would be speculative generality. The `call_tool` indirection would also
> reintroduce the two-protocol split the native-tool-calls plan deliberately removed.
>
> The one piece worth keeping — read-only, Unix-shaped read tools (`rg`, `cat`,
> `ls`, `tree`, `find`) — survives in a different place and for a different reason:
> reading the memory wiki, not discovering executable tools. See the "Read-only Unix
> tools over the memory root" note in `docs/ideas/memory.md`.
>
> The rest of this document is kept for history.

Goal: let the agent discover and understand a growing tool set using familiar
read-only filesystem/search tools and Markdown tool documents.

This plan comes after `docs/plans/2026-06-19 - voice-agent-harness.md`. The core
agent loop should work first with the small current tool list. This plan is the
next scaling step: when the robot has many tools, the agent should not need all
full tool schemas in every model call.

The target shape is:

```text
agent loop
    |
    +-- always-on core robot tools: move_forward, face_me (turn later),
    |       inspect_robot, look_around
    +-- always-on discovery tools: ls, tree, find, rg, cat
    +-- always-on executor: call_tool
    |
    +-- reads docs/agent/tools/**/*.md to discover the rest
    +-- chooses useful executable tools by name
    +-- calls them through call_tool / the harness allowlist
    |
    +-- discoverable robot tools: wiggle, inspect_speaker_direction,
    |       end_session, switch_voice, ...
    +-- discoverable read refinements: head, tail, wc, stat
```

The Markdown files are documentation and discovery. They do not make a tool
executable by themselves. Execution still goes through the harness' real tool
registry.

Follow `AGENTS.md`: keep this plain. Do not build a plugin framework, schema
registry, package manager, or generalized shell.

## Always-On Vs Discoverable

Every agent iteration always carries a small fixed tool set. That set does not
grow as the robot gains capabilities — new robot tools are added as docs and
reached by name through `call_tool`.

Always-on:

- Core robot tools the agent almost always needs:
  - `move_forward`
  - `face_me`
  - `turn` (later, once the IMU is connected)
  - `inspect_robot` (basic body and drive state)
  - `look_around` (camera photo)
- Discovery tools for navigating and reading the tool docs:
  - `ls`, `tree`, `find`, `rg`, `cat`
- The executor:
  - `call_tool`
- The structured `done/final` response.

Discoverable (documented, not always-on, executed via `call_tool`):

- Other robot tools: `wiggle`, `inspect_speaker_direction`, `end_session`,
  `switch_voice`, and anything added later.
- Read refinements: `head`, `tail`, `wc`, `stat`.

The read-tool split is deliberate. The directory tools exist only for tool
discovery, so the always-on read subset is exactly what you need to navigate,
search, and read the doc tree: list, tree, find, search, read. The refinements
(partial reads, counts, metadata) are niceties the agent can reach through
`call_tool` when it wants them. All nine read tools are still implemented with
full, Unix-faithful behavior; only the discovery-focused subset is always-on.

## Non-Goals

- Do not implement memory here. The same read-only tools can search memory later,
  but this plan is only about tool discovery.
- Do not expose arbitrary `bash`.
- Do not expose write tools.
- Do not expose destructive tools such as `rm`.
- Do not let Markdown docs register executable code by being present on disk.
- Do not require this before the first agent harness works.

## Directory Shape

Add a committed tool documentation tree:

```text
docs/agent/tools/
  README.md
  motion/
    move_forward.md
    wiggle.md
    face_me.md
  vision/
    look_around.md
  body/
    inspect_robot.md
    inspect_speaker_direction.md
  session/
    end_session.md
    switch_voice.md
  read/
    head.md
    tail.md
    wc.md
    stat.md
```

The discoverable read refinements (`head`, `tail`, `wc`, `stat`) get docs here so
the agent can find them the same way it finds robot tools. The always-on read
tools (`ls`, `tree`, `find`, `rg`, `cat`) do not need docs to be discovered — they
are already in the prompt — but a short `read/` doc for them is fine if it helps
the agent reason about them.

Generated or runtime-added tool docs can later live under a data directory such
as:

```text
/home/pi/.local/share/robot-pet/agent/tools/
```

V1 should read only the committed docs unless runtime docs are genuinely needed
for manual testing.

## Tool Markdown Format

Keep each file human-readable and easy for an LLM to skim:

```markdown
---
name: move_forward
category: motion
callable: true
danger: moves_robot
---

# move_forward

Move the robot forward a small amount.

Use when:
- The goal requires physical forward motion.
- The robot has a reason to move a short step and then observe again.

Do not use when:
- The user only asked a question.
- The robot needs to turn first.

Arguments: none.

Returns:
- `ok`: whether the request succeeded.
- `result`: usually `started` or `completed`.
- `error`: present when `ok` is false.

Notes:
- This is intentionally a tiny movement.
- For longer movement, call it repeatedly and observe between calls.
```

Do not over-structure the Markdown. The frontmatter is for humans, tests, and
simple filtering. The body is the primary model-facing documentation.

## Read-Only Unix-Shaped Tools

Expose a Unix-shaped read tool set to the agent runner. These exist for tool
discovery: reading the Markdown tool docs.

Make them feel native. Each tool should support the real capabilities of the
underlying command so a model can lean on its existing Unix habits, while still
being bounded by the harness roots, timeouts, and output caps. If a flag the
model would reach for is missing, the tool is not finished.

`ls`, `tree`, `find`, `rg`, and `cat` are always-on. `head`, `tail`, `wc`, and
`stat` are implemented the same way but are discoverable through `call_tool`
rather than always-on (see Always-On Vs Discoverable).

### `ls`

List files and directories.

Arguments:

- `path`
- `recursive`: default false
- `max_entries`: default small, capped

Returns:

- path
- entries with name, type, and size when cheap
- truncated flag

### `tree`

Show a compact directory tree.

Arguments:

- `path`
- `max_depth`: capped
- `max_entries`: capped

Returns a text tree or structured tree. This helps the agent understand a tool
or memory hierarchy before choosing search terms.

### `find`

Find files by name/path pattern.

Arguments:

- `path`
- `name`: glob-style optional pattern
- `type`: optional `file` or `directory`
- `max_results`: capped

Returns matching paths.

### `stat`

Return file or directory metadata.

Arguments:

- `path`

Returns:

- path
- type
- size
- modified time
- readable flag

Use this when the agent needs to decide whether a file is worth reading or when
two similarly named files exist.

### `rg`

Search file contents by shelling out to the real `rg` binary. Mirror ripgrep's
flags and output format so the model's existing ripgrep habits work unchanged.

Arguments:

- `pattern`
- `path`
- `ignore_case`
- `glob`
- `context`
- `files_with_matches`
- `max_matches`

Pass these through to the corresponding ripgrep flags, and accept the common
ripgrep flags directly so the tool feels native. Returns matching path, line
number, and line text. Enforce the root, a timeout, and an output byte cap.

This is intentionally the real `rg`, not a weak "search documents" helper. The
agent should be able to use normal ripgrep search tactics. If the `rg` binary is
absent on the host, return a clear observation rather than silently degrading.

### `cat`

Read file content.

Arguments:

- `path`
- `start_line`: optional
- `max_lines`: capped

Returns text plus line numbers or enough metadata for the agent to request the
next range.

### `head`

Read the beginning of a file.

Arguments:

- `path`
- `lines`: capped

Returns numbered lines from the start of the file. This is mostly a familiar
shortcut for `cat(path, start_line=1, max_lines=...)`.

### `tail`

Read the end of a file.

Arguments:

- `path`
- `lines`: capped

Returns numbered lines from the end of the file. This is useful for append-only
logs or Markdown files where newer entries are near the bottom.

### `wc`

Count lines, words, and bytes.

Arguments:

- `path`

Returns line count, word count, and byte count. This lets the agent estimate
whether to read a whole file or ask for a range.

## Rooting And Safety

The tools are read-only, but they still need boring bounds.

Allowed roots for V1:

- `docs/agent/tools/`

Likely later roots:

- `/home/pi/.local/share/robot-pet/agent/tools/`
- `/home/pi/.local/share/robot-pet/memory/`

Rules:

- Reject paths outside allowed roots after resolving symlinks.
- Reject binary files for `cat`, `head`, `tail`, `wc`, and `rg`.
- Enforce timeouts.
- Enforce output byte caps.
- Return a clear observation when output is truncated.
- Log every search/read call with path and result size.

This still gives the agent Unix-shaped read/search behavior without handing it a
general shell.

## `call_tool`

Expose one executor tool to the agent:

```json
{
  "name": "call_tool",
  "arguments": {
    "tool": "move_forward",
    "arguments": {}
  }
}
```

`call_tool` checks the real harness registry, not the Markdown tree.

`call_tool` can execute any registered tool by name, whether or not that tool is
in the always-on schema set. That is the whole point: the always-on robot tools
can also be called directly, but everything else — `wiggle`,
`inspect_speaker_direction`, `end_session`, `switch_voice`, the `head`/`tail`/
`wc`/`stat` read refinements, and future tools — is reachable only through
`call_tool`. Route by name to the right executor: robot tools go through the
existing `dispatch_tool`, read tools through the read-tool dispatch.

There is no separate registry object to build. The "registry" is the set of
names the harness already knows how to execute (`dispatch_tool`'s tool names plus
the read-tool names). Do not add a registry abstraction or plugin layer for this.

Session tools are in scope. `end_session` and `switch_voice` are documented and
callable via `call_tool`, so a goal can end the session or switch voice. Validate
their side effects the same way the assistant path does.

Rules:

- Unknown tool names return an observation, not a crash.
- Arguments are validated by the real tool handler.
- Tool docs can say `callable: true`, but that is informational only.
- A tool that exists in docs but not in the registry is not executable.
- A registry tool without docs may still be executable, but should be flagged in
  tests so docs do not drift.

## Agent Prompt Changes

Once this plan lands, the agent runner should no longer include every robot tool
schema in every iteration when the list grows. The always-on schema set stays
fixed regardless of how many robot tools exist.

The always-on schema set is:

- Core robot tools: `move_forward`, `face_me`, `turn` (later), `inspect_robot`,
  `look_around`
- Discovery tools: `ls`, `tree`, `find`, `rg`, `cat`
- `call_tool`
- `finish_goal` or the existing structured `done/final` response

Everything else (`wiggle`, `inspect_speaker_direction`, `end_session`,
`switch_voice`, `head`, `tail`, `wc`, `stat`, and future tools) is reached via
`call_tool` after the agent discovers it in the docs.

The prompt should explain:

- Tool documentation lives under `docs/agent/tools/`.
- The core robot tools and discovery tools are already available directly.
- Use `rg`, `find`, `ls`, `tree`, and `cat` to discover other tools.
- Use `call_tool` to execute any documented tool by name, including ones not in
  the always-on set.
- Reading a doc does not execute anything.
- If a tool result suggests a new direction, search/read again.

Keep the current direct tool list available until this flow is proven. The first
patch can support both direct tool schemas and doc discovery. Remove direct
schemas from agent iterations later when the docs path works.

## Phase 1 - Write Tool Docs For Current Tools

### Goal

Create the Markdown registry for the tools that already exist.

### Work

Add docs for every callable tool, both always-on and discoverable. Always-on
tools still get docs so the agent can answer questions about them and so the
consistency test has one source of truth:

- `move_forward` (always-on)
- `face_me` (always-on)
- `inspect_robot` (always-on)
- `look_around` (always-on)
- `wiggle` (discoverable)
- `inspect_speaker_direction` (discoverable)
- `end_session` (discoverable)
- `switch_voice` (discoverable)

Add `docs/agent/tools/README.md` explaining the directory structure.

Keep docs short and operational. The question each file should answer is:

> When should an agent use this, how does it call it, and what will it learn?

### Tests

Add a small docs consistency test:

- Every Markdown file with `callable: true` has a `name`.
- Names are unique.
- Current registry tools have docs, except explicitly hidden/internal tools.
- Docs do not mention tools that cannot be called unless marked future-only.

### Acceptance

```bash
python3 -m unittest tests.test_agent_tool_docs
```

## Phase 2 - Add Read-Only Unix-Shaped Tools

### Goal

Let the agent inspect the tool docs with familiar search/read operations.

### Work

Add `src/voice/read_tools.py` or similar. Keep the name concrete once the code
shape is clear.

Implement all nine, each with full, Unix-faithful behavior:

- `ls`
- `tree`
- `find`
- `rg`
- `cat`
- `head`
- `tail`
- `wc`
- `stat`

`ls`, `tree`, `find`, `rg`, and `cat` are wired into the agent's always-on schema
set. `head`, `tail`, `wc`, and `stat` are implemented the same way but reached via
`call_tool` (Phase 3), so they need docs in the tool tree too. Route read-tool
names to the read dispatch; this is the second executor `call_tool` knows about.

Shell out to the real `rg` binary with a timeout and output caps, mirroring its
flags so the model's ripgrep habits work. Use Python filesystem APIs for the
other read tools, supporting the real capabilities of each underlying command.

Do not support pipes, redirects, shell command strings, command substitution, or
arbitrary environment variables.

### Tests

Cover:

- Allowed-root reads work.
- `..` and symlink escapes are rejected.
- Binary files are rejected.
- Large output is truncated.
- `rg` returns line numbers and paths.
- `head` and `tail` return stable line numbers.
- `wc` and `stat` return useful metadata without reading whole file content into
  the prompt.
- Missing files return useful observations.

### Acceptance

```bash
python3 -m unittest tests.test_agent_read_tools
```

## Phase 3 - Add `call_tool`

### Goal

Let the agent execute a discovered tool by name through the same registry used
by the core harness.

### Work

Add `call_tool` to the agent runner's always-on tool set.

Implementation:

- Look up the name in the names the harness can execute: `dispatch_tool`'s robot
  tools plus the read-tool names. There is no separate registry object to build.
- Route robot tools to `dispatch_tool` and read tools to the read dispatch.
- Validate arguments the same way direct tool calls are validated.
- Return the same structured observation as direct calls.
- Session tools (`end_session`, `switch_voice`) are reachable through `call_tool`.

### Tests

Cover:

- `call_tool("move_forward", {})` reaches the fake motion caller.
- Unknown tool returns `ok=false`.
- Bad arguments return a validation observation.
- Tool docs are not consulted for execution permission.

### Acceptance

```bash
python3 -m unittest tests.test_voice_agent_runner tests.test_agent_tool_docs
```

## Phase 4 - Teach The Agent To Discover Tools

### Goal

Move the agent from "all schemas are always visible" toward "discover docs, then
call by name."

### Work

Update the agent prompt:

- Start each goal knowing the always-on set: core robot tools, discovery tools,
  and `call_tool`.
- Tell the model where tool docs live and that the always-on robot tools are
  already directly available.
- Encourage `rg` first for vague needs and `cat` for exact docs when reaching for
  a tool outside the always-on set.
- Encourage observing after physical tools.

The core robot tools stay always-on permanently — moving and basic inspection are
the agent's bread and butter, so they should never require a doc lookup. What
gets removed from the loop over time is the long tail of less-common robot tools,
not the core set. For the first implementation, keep those tail schemas available
as a fallback. Log whether a goal used doc discovery. Once real use looks good,
remove the tail schemas from the agent loop, not from the normal assistant path.

### Tests

Use fake model responses:

- Search docs for a tool outside the always-on set (for example `wiggle`).
- Read its doc.
- Call it via `call_tool`.
- Observe.
- Finish.

Also test:

- Search docs for an unavailable tool.
- Agent recovers by searching another term.

### Acceptance

```bash
python3 -m unittest tests.test_voice_agent_runner tests.test_agent_read_tools tests.test_agent_tool_docs
```

## Manual Validation

Run after the core voice agent harness is already working.

1. Ask: "what tools can you use to move?"
   - Expected: agent searches docs and answers from Markdown, without moving.

2. Ask: "move toward me and stop when close."
   - Expected: agent searches or reads relevant docs, then calls motion and
     observation tools through `call_tool`.

3. Ask: "look for a way to inspect your body status, then tell me if you can
   drive."
   - Expected: agent discovers `inspect_robot`, calls it, and answers from the
     result.

4. Temporarily remove or rename one doc.
   - Expected: corresponding registered tool can still run if called by name,
     but docs consistency tests catch the drift.

## Future Extensions

The same read-only tools can later point at memory roots. That should be a small
allowed-root addition, not a redesign:

```text
docs/agent/tools/
/home/pi/.local/share/robot-pet/memory/
/home/pi/.local/share/robot-pet/agent/tools/
```

Generated tools can later add Markdown docs too, but execution must still require
registration in the real harness tool registry.
