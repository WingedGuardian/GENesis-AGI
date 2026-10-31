- **The `genesis-merge-gate` check appears once per PR head.** The merge-gate
  workflow's job now runs under its own check name, `merge-gate-run`, and always
  succeeds; every event, including the PR's own push, publishes the verdict as the
  `genesis-merge-gate` check. Before, the push-triggered job carried that name
  itself and was usually cancelled by the review events that followed, so most PRs
  listed a cancelled `genesis-merge-gate` beside the real verdict.
