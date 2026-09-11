- **Genesis now records which live session is using which worktree, instead of
  guessing.** With many sessions sharing one repository, nothing said who was
  where, so the daily cleanup could archive a worktree a session was still
  working in — leaving that session broken and its unsaved work recoverable only
  by hand. Ownership is now written down, using git's own "this worktree is in
  use" marker, which the cleanup job already respects. Recording the fact is the
  whole protection; nothing new enforces anything.
- **Both signals the cleanup job had for "is anyone using this" turned out not to
  work, and that is why this exists.** The first looked for a session whose
  working directory was inside the worktree; sessions change directory per
  command, so their working directory never leaves the main checkout — measured
  as none of two hundred worktrees, while seven sessions were running. The second
  asked how recently anything in the worktree changed, but only looked at the top
  two levels of directories, and editing a file updates only that file. A
  worktree whose source had just been edited still reported nineteen days idle
  and eligible for cleanup; git could see the edit, the check could not. Nearly
  all of this project's source sits below the level that check looks at, so this
  was the ordinary case rather than an edge one.
- **Exactly one thing is recorded: which live process is using the worktree.**
  Whether there is unsaved work is deliberately NOT recorded — the cleanup job
  can determine that itself, at the moment it decides, and does. Writing it down
  in advance would mean checking it in one place and acting on it somewhere else,
  and everything that had to be built to keep those two in agreement turned out
  to be where the problems came from.
- **A marker left by something else is reported, never touched.** A hand-written
  one, or one belonging to another tool, is left exactly as found, and ours
  carries an identifier specific enough that ordinary notes cannot be mistaken
  for it. Claims are tied to a process and to when that process started, so a
  recycled process number cannot be mistaken for the original owner, and a claim
  that cannot be given a condition for lifting is never written at all.
