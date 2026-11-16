- Issue #4519 is documented as a two-part queue workflow: Part 1 supplies a
  read-only hourly worklist; each closing pass starts with `--show`, quotes its
  age and coverage, uses it to select work, and reruns the actual PR gate before
  acting. A missing, partial, or failed cache falls back to a complete GitHub
  listing. A parked PR uses `parked` plus a maintainer `## Parked` reason and
  return condition; label-only rows warn, comment age is shown when present, and
  the sweep never auto-wakes parked work or writes to GitHub. Live install and
  timer verification remain pending.
