- **The development skill now covers why a PR's CI can be missing, red, or
  lying.** Four diagnosis traps that each produced a wrong conclusion in one
  session, added beside the existing conflict-suppression trap rather than
  duplicating it: mergeability reading `UNKNOWN` after any merge (and so
  suppressing CI queue-wide for a while); a PR opened against a non-default base
  never running CI at all, because the workflow filters on the base and
  retargeting emits no triggering event; `workflow_dispatch` completing without
  ever appearing in the PR's check rollup; and `gh run rerun` replaying the
  original merge commit, so it cannot clear a failure caused by a base that has
  since been fixed.

  Also recorded: `gh run view --log-failed` truncates on a large suite and shows
  only PASSED lines, so a failing job reads as a clean log; and the class where
  two individually-green pull requests break `main` together, because one adds a
  repo-wide invariant test and the other adds a violation of it, with no textual
  conflict between them and no signal until both have merged.

- **A verification keyed on existence can only confirm.** The skill's
  verify-outcomes guidance now names the shape directly: a check that would still
  pass if the thing were absent, stale, or the wrong object has proven nothing.
  Three worked examples, all of the same shape — waiting for a new artifact by
  testing that any exists, matching the first row rather than the intended one,
  and counting from a listing sorted by a key other than the one being counted.
