- **A red CI `test` job now names its failures somewhere log truncation
  cannot reach.** The job printed one line per test across ~26,000 tests, and
  GitHub's log APIs drop the tail of an oversized step log — which is exactly
  where pytest prints its FAILURES block. Measured on a real red run: the
  log, the failed-step view, the whole-run log and the raw endpoint all cut
  at ~44% of the suite, so the run named no failing test through any route,
  and the junit report that held the names was discarded with the runner.
  Diagnosis of that failure required compiling the runner's git version from
  source to reproduce locally — a cost this change exists to make nobody pay
  twice.

  Three changes, one per layer: pytest drops `-v` for `-rfE` (compact output,
  with both failures AND errors listed in the terminal summary — an error is
  a different outcome from a failure, so `-rf` alone would leave every
  collection-time breakage named nowhere); a failure-only step parses the
  junit report and writes every failed/errored test id — whole, with the
  first line of its message — to the job's step summary, which the Actions
  UI renders outside the log entirely; and the junit report itself is
  uploaded as a run artifact so the full tracebacks survive the runner. The
  summary script always exits 0 — the job is already failing, and a narrator
  that can fail replaces the real failure with its own — with missing and
  unparseable reports carried as printed notices, including the case where
  pytest died before writing any report at all.

  The step summary is bounded to GitHub's 1 MiB cap, which is a hard external
  budget rather than a preference: a summary over it is not rendered, so an
  unbounded list would print nothing at all and take the artifact pointer
  with it. The budget is spent on WHOLE rows, the closing pointer is reserved
  before any row is written, and a shortfall is stated with its denominator —
  measured at 12,000 failures, the summary lands at 1,048,288 bytes with
  7,674 rows listed and the remaining 4,326 declared.
