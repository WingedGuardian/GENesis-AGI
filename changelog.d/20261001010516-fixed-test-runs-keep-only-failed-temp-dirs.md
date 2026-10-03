- **Test runs no longer keep every passing test's temporary directory.**
  The pytest configuration now sets `tmp_path_retention_policy = "failed"`,
  so a passing test's `tmp_path` is removed when the test finishes and only
  failing tests keep theirs for debugging. Runs that pass their own
  `--basetemp` previously kept every directory until removed by hand.
