- **`scripts/wait_main_ci.py` waits for main CI's verdict on a merge.** Main
  never cancels its CI run in progress, but each new push replaces the run still
  pending, so a merge's own run is often never run. The script takes the newest
  run that contains the merge and concluded as a pass or a fail, never a
  cancelled or signal-less one, and exits 0 (pass), 1 (red, with the failing
  jobs), 2 (timed out) or 3 (the commit cannot be checked). The closing-session
  skill now names it, and adds the rule for a jammed main CI: batch ready,
  green PRs that share no files, then wait for one run that contains them all.
