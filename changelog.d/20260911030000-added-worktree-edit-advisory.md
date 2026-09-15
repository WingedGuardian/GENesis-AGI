- **A session is now told when it edits inside a worktree another live session is
  working in.** It is a note, never a refusal: the edit goes through, and the
  message says who holds the worktree and suggests checking with them or moving
  to your own. This is the case that had no guard at all — the existing
  protections only ever covered deleting a worktree, not writing into one, and it
  came within one instruction of happening to a worktree holding more than a
  hundred lines of another session's unsaved work.
- **Ownership is claimed on the first edit into a worktree, not only when one is
  created.** Two hundred worktrees already exist and sessions mostly pick up an
  existing one rather than make a new one, so claiming only at creation would
  leave the common case unclaimed — and a warning that can never fire is worse
  than none, because it reads as coverage. Nothing is required of you; the claim
  is taken and released automatically.
- **The note is delivered on the only channel that reaches the session.** An
  earlier version of this wrote it to the error stream, which for this kind of
  hook goes to a debug log nobody reads — so the warning existed and never
  arrived. It was checked by capturing that output directly, which proves
  something was produced and nothing about whether anyone receives it. The two
  are not the same test, and only the second one matters.
- **A bug in this advisory can no longer refuse an edit.** It was wired to a
  helper that turns any unexpected error into a refusal, which is right for the
  guards that protect irreversible actions and wrong here — that helper's own
  documentation says so. It now fails open: a failure is reported and the edit
  proceeds.
- **A correction to what the older guard's own description promised.** It
  advertised blocking a deletion when another session had that worktree as its
  working directory. The code does check that, but the check cannot fire in
  practice — sessions change directory per command, so their working directory
  never leaves the main checkout, measured as none of 200 worktrees on a live
  install. What actually makes deletion safe there is the blanket refusal below
  it. The description now says so, because believing otherwise is how two guards
  came to be built on a signal that was never present.
- **Claims no longer reach outside this project.** Ownership was worked out from
  where a file sits on disk, which cannot tell one project's worktree from
  another's — so a session working here, handed a path inside an unrelated
  checkout, would have written this project's lock into that other project,
  where it would pin a worktree the owner's own tools then refuse to remove.
  Ownership is now decided by which repository a worktree actually belongs to,
  and anything outside this one is left alone. Where the question cannot be
  answered, the answer is "not ours": a missed note costs a warning, a wrong
  claim costs someone else's repository.
- **Turning worktree ownership off no longer strands the claims it already
  took.** The shipped settings file says that switching it off leaves existing
  claims alone precisely because they release when their session exits — but the
  off switch was checked first, so a session that ended after the setting changed
  released nothing. The claims then sat there permanently, skipped by the
  cleanup, with the feature that made them switched off and unable to clear up
  after itself. Releasing now happens regardless of the setting, which is what
  the documentation already promised. Claiming and warning are still switched
  off, as intended.
- **An environment that points git at a different project can no longer make
  someone else's worktree look like ours.** These overrides take precedence over
  asking about a specific directory, so both halves of the ownership question
  returned the same foreign answer and agreed — and agreement is exactly what
  reads as "this is ours". The check did not merely fail, it inverted. The
  project's own hook launcher already strips the same variables for the same
  reason.
- **A worktree whose folder name contains a line break is now released
  properly.** Line breaks are legal in names on this platform, and one inside a
  name split the listing the release step reads, producing a truncated name that
  matches nothing — so that worktree was never visited and its claim outlived the
  session. The listing is now read in a form that cannot be split by the names
  inside it.
- **The settings text now matches what is actually switched on.** Both
  operator-facing descriptions still said this shipped a library only and that
  nothing took a claim, which stopped being true when the hooks were registered
  — so the settings view could tell an operator that an enabled lever gated
  nothing while it was live. They now say what runs and, just as importantly,
  what does not: a session killed before it can release leaves a claim that must
  be cleared by hand until the cleanup-side backstop lands.
- **A session is recognised however it was launched.** The check accepted only
  one executable name, so a session started through a configured path — which
  presents a different name on this platform, one the project's own process
  classifier already accepts — was not recognised as a session at all. The
  consequence was not a missing warning but a missing claim: nothing was
  recorded for that session, so the feature was silently off for it.
- **A valid session identifier is no longer discarded.** The identifier was
  checked against a pattern narrower than the one this project treats as
  canonical, so ordinary identifiers were dropped and the collision note fell
  back to "another session" while it had the name in hand. It now uses the
  shared check rather than a fourth private copy of it.
- **Taking and releasing a claim no longer trusts the ambient environment.** The
  earlier fix cleaned the question — which project does this worktree belong
  to — but not the action that followed it, so a stray environment setting could
  still send the write somewhere the check had nothing to do with. The decision
  and the action now agree about which project they mean.
