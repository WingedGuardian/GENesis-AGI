- **The merge gate no longer reads CI red when a later workflow run passed.** A
  PR whose CI failed (for example on a broken base branch) and then passed in a
  new run on the same head used to stay `ci: red`, because the check rollup keeps
  every run. A FAILURE or TIMED_OUT check is now superseded only by a later
  SUCCESS of the same job and workflow from a newer run of the same repository;
  an older run's success, including a re-attempt of an older run, never clears a
  newer run's failure. Anything that cannot be proven (no run id, another
  repository, no timestamp, a tie) stays red.
