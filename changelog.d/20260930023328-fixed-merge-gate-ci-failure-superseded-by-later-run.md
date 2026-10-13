- **The merge gate now reads CI from the newest run of each workflow.** A PR
  whose CI failed (for example on a broken base branch) and then passed in a
  new run on the same head used to stay `ci: red`, because the check rollup
  keeps every run. For each workflow, the newest run on the head now decides as
  a whole: a newer passing run clears an older run's failures, and a newer run
  that failed, was cancelled or is still running decides the result in the same
  way. Results are never mixed across runs, so two failed runs cannot add up to
  a pass. A newer run that skips a job also clears that job's failure from an
  older run. A check whose run cannot be identified is always counted. This does
  not block re-running a failed job until it passes: GitHub reports only a
  re-run's latest attempt.
