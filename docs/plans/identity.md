> DO NOT CONSIDER THESE TO BE CONCRETE PLANS. ANY OPINIONS HERE ARE HALLUCINATED BY AI.

# Identity / Person Registry Plan

## Goal

One place that answers "who is this person?" — a single record per known person
that the recognizers and the personality layer share. Today [[facematch]],
[[voice-match]], and [[multi-person-conversation]] each describe their own
gallery (face crops, voice embeddings, session-local placeholders). Those are the
same person seen through different sensors.

A person registry holds one record per person; the recognizers write into it and
read names out of it, and [[memory]] / [[personality]] attach to it.

## What a record holds

- **Identity.** A stable person id and a display name.
- **Face references.** Crops or embeddings for [[facematch]].
- **Voice references.** Embeddings / samples for [[voice-match]].
- **Relationship state.** Familiarity, running bits, last seen — feeds the
  [[personality]] state block.
- **Provenance.** When/how each reference was captured.

Memory facts about a person point at the person id rather than copying the name,
so a rename or merge updates everything.

## Mechanics

- **Recognizers stay pure.** [[facematch]] turns a crop into "person id X (or
  unknown)"; [[voice-match]] turns an utterance into "person id Y (or unknown)."
  Neither owns the gallery — they query and update the registry.
- **One enrollment path.** "This is Sam" creates or updates a record; a face crop
  and/or a voice sample captured at that moment attach to the same id.
- **Unknown handling.** Unidentified faces/voices get a stable session-local
  placeholder; the registry decides whether a placeholder graduates into a saved
  record.

## Open questions

1. **One record vs per-sensor stores.** Is there a single person record spanning
   face + voice, or separate galleries keyed by a shared id?
2. **Merging.** When face says person A and voice says person B for the same
   speaker, who wins, and how do two records get merged?
3. **Promotion.** When does a session-local "Speaker 2" become a saved person —
   only on explicit naming, or after enough sightings?
4. **Storage & lifecycle.** Where the registry lives (e.g. under
   `/home/pi/.config/robot-pet/`), which service owns it, and whether it survives
   the ROS2 migration like the drivers do.
5. **Ownership at runtime.** Is the registry a library the voice/vision services
   both link, or a small service they call?
6. **Editing.** Surface records in the web dashboard for rename / merge / delete?

## Relationship to existing code

This is the shared substrate under [[facematch]], [[voice-match]], and
[[multi-person-conversation]]; it resolves the "where do references live / one
record or separate" question each of those raises. The recognizers and the
conversation layer depend on it; it depends on none of them.
