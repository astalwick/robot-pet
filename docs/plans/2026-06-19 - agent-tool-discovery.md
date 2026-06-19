# Agent Tool Discovery Plan

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
    +-- always-on read-only tools: ls, tree, find, rg, cat, head, tail, wc, stat
    +-- always-on executor: call_tool
    |
    +-- reads docs/agent/tools/**/*.md
    +-- chooses useful executable tools by name
    +-- calls them through the harness allowlist
```

The Markdown files are documentation and discovery. They do not make a tool
executable by themselves. Execution still goes through the harness' real tool
registry.

Follow `AGENTS.md`: keep this plain. Do not build a plugin framework, schema
registry, package manager, or generalized shell.

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
```

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

## Always-On Read-Only Tools

Expose a small Unix-shaped tool set to the agent runner.

These should feel familiar to models that know Unix commands, while still being
bounded by the harness.

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

Search file contents. Use ripgrep internally when available.

Arguments:

- `pattern`
- `path`
- `ignore_case`
- `glob`
- `context`
- `files_with_matches`
- `max_matches`

Returns matching path, line number, and line text. Cap output bytes.

This is intentionally closer to `rg` than a weak "search documents" helper. The
agent should be able to use normal search tactics.

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

Rules:

- Unknown tool names return an observation, not a crash.
- Arguments are validated by the real tool handler.
- Tool docs can say `callable: true`, but that is informational only.
- A tool that exists in docs but not in the registry is not executable.
- A registry tool without docs may still be executable, but should be flagged in
  tests so docs do not drift.

## Agent Prompt Changes

Once this plan lands, the agent runner should no longer include every robot tool
schema in every iteration when the list grows.

Start with:

- `ls`
- `tree`
- `find`
- `rg`
- `cat`
- `head`
- `tail`
- `wc`
- `stat`
- `call_tool`
- `finish_goal` or the existing structured `done/final` response

The prompt should explain:

- Tool documentation lives under `docs/agent/tools/`.
- Use `rg`, `find`, `ls`, `tree`, `cat`, `head`, `tail`, `wc`, and `stat` to discover relevant tools.
- Use `call_tool` to execute a documented tool by name.
- Reading a doc does not execute anything.
- If a tool result suggests a new direction, search/read again.

Keep the current direct tool list available until this flow is proven. The first
patch can support both direct tool schemas and doc discovery. Remove direct
schemas from agent iterations later when the docs path works.

## Phase 1 - Write Tool Docs For Current Tools

### Goal

Create the Markdown registry for the tools that already exist.

### Work

Add docs for:

- `move_forward`
- `wiggle`
- `face_me`
- `look_around`
- `inspect_robot`
- `inspect_speaker_direction` if added by the harness plan
- `end_session`
- `switch_voice`

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

Implement:

- `ls`
- `tree`
- `find`
- `rg`
- `cat`
- `head`
- `tail`
- `wc`
- `stat`

Use simple subprocess calls for `rg` if that stays clear, with timeout and output
caps. Use Python filesystem APIs for the other read tools.

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

- Look up the name in the real tool registry.
- Validate arguments the same way direct tool calls are validated.
- Execute through the same dispatcher as Phase 1 of the harness plan.
- Return the same structured observation as direct calls.

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

- Start each goal knowing only the always-on tools.
- Tell the model where tool docs live.
- Encourage `rg` first for vague needs and `cat` for exact docs.
- Encourage observing after physical tools.

For the first implementation, keep direct schemas available as a fallback. Log
whether a goal used doc discovery. Once real use looks good, remove direct
schemas from the agent loop, not from the normal assistant path.

### Tests

Use fake model responses:

- Search docs for "move".
- Read `move_forward.md`.
- Call `move_forward`.
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
