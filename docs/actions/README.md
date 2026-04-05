# Action Item Tracking

Canonical location for action items produced by evaluations, conversations,
and autonomous sessions. Organized by owner (Genesis development vs user
personal) and lifecycle state.

## Structure

```
docs/actions/
  README.md              ← this file (git-tracked)
  genesis/               ← Genesis development action items (gitignored)
    active.md            ← currently being worked or next up
    completed.md         ← done items with implementation references
    deferred.md          ← explicitly parked with reason and scope
  user/                  ← user personal action items (gitignored)
    active.md            ← items Genesis is tracking for the user
    completed.md         ← done items
```

## Conventions

### Item Format

```markdown
### [Item title]
- **Source:** [evaluation doc, conversation, or inbox item that spawned this]
- **Date:** YYYY-MM-DD
- **Description:** What needs to be done
- **Implementation ref:** [commit/PR/file — added when completed]
- **Outcome:** [brief — did the evaluation's prediction hold up? Added on completion]
```

### What Goes Here

- Concrete action items from `/evaluate` and `/user-evaluate` evaluations
- Tasks identified during foreground conversations
- Items flagged by inbox evaluations
- To-do items the user drops in the inbox

### What Does NOT Go Here

- Architecture decisions → `docs/architecture/`
- Design documents → `docs/plans/`
- Historical records → `docs/history/`
- Session learnings → memory system (MEMORY.md + memory files)

### Lifecycle Rules

- **No binding metadata.** Items are facts (what, where from, when), not opinions.
  Genesis may suggest priority/timeline in evaluation reports, but those suggestions
  are NOT stored on the items themselves.
- **Move, don't delete.** When an item is completed, move it to `completed.md` with
  an implementation reference and outcome note. When deferred, move to `deferred.md`
  with a reason and scope note (V4/V5/Future).
- **Declined items stay in source docs.** If an evaluation produced an item that was
  explicitly declined, leave it in the original evaluation document as a historical
  record. Don't migrate declined items here.

### Who Writes Here

- **Foreground CC sessions** — can write directly after evaluations or conversations
- **Ego/autonomous sessions** — can propose items via message_queue; foreground picks up
- **Inbox evaluations** — cannot write (Write tool disallowed), but recommend items in
  their response: "Action item identified — should be added to `docs/actions/genesis/active.md`"

### Feedback Loop

The `Outcome` field on completed items is the feedback loop. Over time, grep
`completed.md` to see which evaluations produced the most useful work and whether
predictions held up.
