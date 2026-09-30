- **The merge gate now reads each CI check by its latest result.** A PR whose CI
  failed (for example on a broken base branch) and then passed in a new run on
  the same head used to stay `ci: red`, because the check rollup keeps every run.
  For each check (same job name and workflow), the result that completed last
  now decides: a later success clears an earlier failure, and a later failure or
  cancellation overturns an earlier success. Results that finished in the same
  second are all kept, so any failure among them still reads red, and a check
  whose completion time is unknown is never cleared. A newer run that skipped a
  job does not clear that job's earlier failure. This does not block re-running
  a failed job until it passes: GitHub reports only a re-run's latest attempt.
