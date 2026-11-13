- **Specs are checked before a builder gets them, and builds are checked
  against their spec before the PR.** A read-only analyze pass reports
  duplicated or conflicting requirements, unmeasurable wording, references
  nothing defines, uncovered requirements and conflicts with standing rules (a
  conflict is always CRITICAL). The `spec-session` skill runs it before handing
  a spec over, and the architect agent runs it on any plan or spec it reviews.
  A converge pass lists what a build still owes its spec, and `build-session`
  runs it before opening a PR. Both are adapted from GitHub's spec-kit (MIT).
