- The merge gate now runs as an advisory GitHub check (`genesis-merge-gate`,
  workflow `merge-gate`) on every pull request, review, and comment, producing
  the same `git_push_guard.py --check-pr` report installs run at merge time —
  phase 1 of issue #1670. It is deliberately NOT a required check; promotion
  follows the criteria documented in `.github/workflows/merge-gate.yml` after
  its false-block rate is measured. While running under Actions the gate
  excludes its own workflow's checks from the CI rollup so the check can never
  vouch for or block on itself (a check inspecting its own pending run would
  deadlock); the interactive merge path is unchanged.
