- **Build sessions now read a reuse-first ruleset before writing code.** The
  `build-session` skill vendors the Ponytail ruleset (MIT): before new code,
  check whether the change needs to exist, then whether the repo, the standard
  library or an installed dependency already does it. A house delta keeps the
  repo's own bars: GROUNDWORK code stays, named seams are not cut, the test
  rules stay in full, and a new dependency still goes through `evaluate`.
