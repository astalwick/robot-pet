> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Robot Memory Plan

## Goal

Give Bloop memory: things it learned in past conversations or observed about its
world that survive a `robot-voice` restart and can be recalled in a later turn.

Examples of what "having memory" should feel like:

- "You told me yesterday you don't like the vacuum." (recall a past statement)
- "Arlen is the one with the deep voice who sits on the left." (a person fact)
- "The kitchen is the room with the loud floor." (a place fact)
- "Last time I tried to go forward here I hit something." (an experience)

This is the persistent counterpart to the in-session conversation history that
already lives in `src/voice/conversation.py`. That history dies with the
session; memory does not.

## Candidate shapes

### A. Filesystem wiki (flat markdown facts)

One fact per markdown file with light frontmatter (`subject`, `kind`,
`last_seen`). The LLM reads relevant files into the prompt at session start or
via a `recall` tool. Mirrors how the assistant's own agent memory works.

- No embeddings, no database; greppable and editable by hand.
- Could fold into the personality system prompt as a memory block. See
  [[personality]].
- Doesn't scale past a few hundred facts; "which files are relevant?" becomes
  keyword matching.

### B. Single structured store + recall/remember tools

One JSON file of short facts with tags/subjects. Two LLM tools:
`remember(fact)` and `recall(query)`. `recall` does keyword/substring match.

- Local, no embeddings; scales further than loose files.
- An explicit write step means the model decides what to keep.
- Keyword recall misses paraphrase ("the loud room" vs "kitchen").

### C. Very simple local RAG

Embed each fact, store vectors locally, retrieve top-k by cosine on the user's
current utterance, inject into the prompt. Embeddings from a small local model
or a cheap API call.

- Handles paraphrase and "vibe" recall.
- More moving parts: embedding model choice, index freshness, an extra
  dependency on the Pi.

## Brainstorm — directions

- **End-of-session distiller.** A summarizer runs when the session ends, reads
  the conversation history (`src/voice/conversation.py`), and emits a few durable
  facts. Separates "what was said" from "what's worth keeping."
- **Salience tagging.** Persist only facts the model marks durable (preferences,
  identities, places, experiences); drop transient chit-chat.
- **Dedup/merge on write.** Before adding a fact, look up existing facts on the
  same subject and update / bump `last_seen` instead of appending a near-duplicate.
- **Two-tier recall.** A small "hot" set (a handful of high-salience facts) always
  injected into the prompt, plus a "cold" store hit on demand via `recall`.
- **Tiered retrieval.** Keyword/substring first; fall back to embeddings only when
  keyword recall returns nothing — keeps the embedding cost off the common path.
- **Provenance.** Store who/when each fact came from so recall can say "you told me
  Tuesday." Subject keyed to a person id from [[facematch]] / DoA when known.
- **Contradiction handling.** When the user contradicts a stored fact, tombstone or
  downweight it rather than silently keeping both.
- **Dashboard review.** Since the filesystem-wiki shape is greppable and editable,
  surface stored facts in the web dashboard for inspection / correction / deletion.

## Open questions

1. **Write policy.** Who decides what gets remembered — the LLM via a tool, a
   background summarizer at end-of-session, or both?
2. **Recall trigger.** Always load a small memory block into the system prompt,
   or an on-demand `recall` tool, or both?
3. **Forgetting.** Decay by `last_seen`? Cap total facts and evict oldest?
4. **Identity / people.** Memory about *who* is talking depends on recognizing
   them. Couples to [[facematch]] (vision), [[voice-match]], and DoA
   (`src/voice/doa.py`). Per-speaker attribution comes via
   [[multi-person-conversation]].
5. **Self-introspection.** Should the robot remember facts about *itself* (it
   bumped something, the battery died mid-turn)? Overlaps with telemetry and the
   Phase 4 "state layer" in [[personality]].
6. **Storage location & lifecycle.** Where it lands (e.g. under
   `/home/pi/.config/robot-pet/`), which service owns it, and whether it needs
   to survive the ROS2 migration like the drivers do.
