# Release review

- **A RELEASE review is scoped to what the release CHANGES; a whole-codebase
  audit gates nothing.** Point a reviewer at "the repository" and it returns
  findings that PRE-DATE the release — real, worth fixing, and not release
  blockers. Treating them as blockers is how a release never ships. Scope the
  gating pass to the diff between the previous RELEASE tag and HEAD (this repo
  also carries non-release tags, so `git describe --tags` alone can land on the
  wrong one); run the broader
  audit separately, on its own clock, where its findings become ordinary work
  rather than a stop. The distinction is not "which findings are real" but
  "which findings this release is answerable for" — a defect that already
  shipped in the last release is answerable by the next fix, not by this gate.
  Release scope decides what BLOCKS, never whether an external round COUNTS: a
  pre-dating finding is still a defect-bearing round on the escalation counter,
  filed as ordinary work rather than held as a release blocker. "It pre-dates
  the release" is otherwise just a new spelling of the rationalization that
  counter exists to kill.

- **A pre-existing defect the change RECRUITS FOR is IN-arc — FOR A RELEASE
  REVIEW.** The bullet above splits findings into "this release's" and
  "pre-dating it", and there is a third bucket between them that neither
  catches: a defect that already existed and which THIS change newly leads
  people INTO — because it adds the documentation that sends them down that
  path, flips the default that reaches it, or ships the feature whose obvious
  next step lands on it. It is not in the diff, and the RELEASE is still
  answerable for it, because the release is what made it reachable. Ask of every
  pre-existing finding: *did we just build the road to it?* If yes, it gates
  **the release**. (Adopted from a sibling toolkit's release-review protocol,
  which carries a dated case of exactly this shape: a latent defect in an
  install path that was harmless until a release's own docs began recommending
  that path.)

  **This does NOT override "Keep the PR the PR"** (standing user rule,
  2026-09-09, closing-session, "Keep the PR the PR"). On an individual PR the routing question
  is unchanged — *does the PR work without this fixed?* — and for a recruited
  defect the answer is USUALLY yes, so it is FILED as an issue while the PR
  merges. Run the question anyway rather than assuming the answer: a change that
  flips a DEFAULT onto the latent path fails it, and a PR that breaks on first
  use is not a merge. What this bullet changes is the RELEASE's answerability,
  not whether a PR waits. Holding a PR for a recruited defect is the exact
  friction the owner removed, so if you find yourself about to, you have
  mis-scoped this bullet.
