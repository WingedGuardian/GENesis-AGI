# Ponytail: where Genesis differs

Read this before `ponytail.md`. Where the two disagree, this file wins, and
anything `CLAUDE.md`, `genesis-development` (including
`references/commit-path.md`) or `build-session` requires wins over both.
Ponytail decides how LITTLE code to write. It never lowers a bar this repo
sets. Each item below names the upstream line it overrides.

- **"Lazy" means less code, never less care.** Upstream's "You are a lazy
  senior developer" is about how much new code you write. It is never lazy
  about root causes, finishing the change, or checking it: `CLAUDE.md` says
  "No laziness... No temporary fixes. No shortcuts."
- **No `shortcut:` comments.** Upstream's "A shortcut with a known limit gets a
  code comment in this form: `shortcut: <the limit>, <when to upgrade>`" is not
  adopted. A comment is not a tracked record. A known limit you cannot remove
  in this item gets a GitHub issue, cited in the PR, or a
  `# GROUNDWORK(<issue>): <why>` tag when it marks deliberate future work.
- **Tests follow the house rules, not the size of the change.** Upstream's
  "one small test or an assert-based self-check. Trivial changes need none" is
  not adopted. Whether a change needs a test, and what kind, follows
  `commit-path.md` (a reproduction test first for a bug, verify-RED for every
  new test) and the genesis-development anti-rationalization table ("This is
  just a simple fix, no tests needed": write the test). Guard and hook tests
  and mutation sweeps apply where those rules require them. An assert in the
  code never replaces a test.
- **The closing line names decisions, not debts.** Upstream ends each reply
  with "what you skipped or did not check, and any risk". Do that, with these
  limits:
  - a bug, defect or in-scope gap you skip is fixed now or filed as an issue
    (`CLAUDE.md`, "Where deferred work goes"), and the line gives the issue
    number;
  - upstream's one-or-two-line cap never shortens a zero-drop handoff, which
    names every unfinished piece;
  - when the work ends in a PR, the same lines go in the PR body's
    `## Testing` section.
- **"Not scope" means not built, not unmentioned.** Upstream's "Extra features
  are not [scope]" and "Skip features... nobody asked for" decide what this
  item BUILDS. What you notice that nobody asked about still gets surfaced to
  the owner, as `CLAUDE.md`'s co-pilot rule asks.
- **GROUNDWORK stays.** A `# GROUNDWORK(<id>): <why>` tag marks code kept on
  purpose. Upstream's "Deletion beats addition" never removes it, or the code it
  marks, and applies only inside this item's scope, never to unrelated code.
- **A named seam is not an unrequested abstraction.** An adapter, interface,
  plugin point or settings lever that the plan, the spec or `CLAUDE.md` asks
  for ("Flexibility > lock-in", "A settings lever for every autonomous
  behavior") is part of the task. Rung 1 ("does it need to exist?") applies to
  what nobody asked for.
- **Never cut for brevity**, beyond upstream's own "Never cut" list:
  validation, error handling, security and privacy checks, observability (the
  logging, events and records a later reader needs), and the loud marker on
  any bound ("Select, don't amputate"). A shorter diff that drops one of these
  is unfinished, not smaller.
- **ADOPT before ADAPT before build.** Rungs 2 to 4 (this codebase, the
  standard library, an installed dependency) come first, as upstream says.
  Before rung 6, when the capability is more than a few lines and touches the
  outside world, stop and run the `evaluate` skill. Upstream's "Never add a
  dependency for a few lines" holds only for a few lines, and a NEW dependency
  of any size still goes through `evaluate` first.
- **Finding callers.** For upstream's "grep every caller", use Serena's
  `find_referencing_symbols` (live) for Python symbols, and grep for text.
- **Level.** The rules apply at upstream's `full` level to every code edit in
  a build item. There is no `/ponytail` command here. The owner's explicit
  instruction in the session can still change or switch off the rules;
  nothing else does.
- **Comments.** Match the density and style of the surrounding code, and keep
  what a later reader needs: `GROUNDWORK` tags, and the reasoning or the
  measurement behind a bound or a magic number. Upstream's "comment only the
  why, in one line" is the default for new code with no surrounding pattern.
