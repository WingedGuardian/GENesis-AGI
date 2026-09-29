- **The inbox now acts on its URL-coverage check, and tells you when it gives up
  on something.** An evaluation that never cites one of its input URLs as a
  `**Source:**` field is re-queued instead of being marked done. This check ran
  in observe-only mode until compliance was measured: across the first 42
  evaluations under the citation rule, every one cited its sources and none would
  have been re-queued. To observe without acting, set `url_coverage_mode: shadow`.
  Previously, an item that used up its retries, or a file that tripped the
  repeated-failure guard, simply stopped being evaluated, with no signal. Now you
  get an alert naming the file (by its path inside the inbox folder) and, where
  individual items stopped, each of them, with links shown without query strings
  or token-like path segments. The guard's retry lane now stops the file's URL
  retries instead of re-checking it on every scan. An approval request that ends
  unanswered no longer uses up one of the item's retries, which could leave the
  item never evaluated again. A `**Source:**` line inside a list item or
  blockquote, including nested ones such as `- > **Source:**`, now counts as a
  citation. With network parking live (`resilience.parking_mode: live`), an
  outage no longer spends an item's retries. Invalid `items_per_eval`,
  `max_retries`, `timeout_s` and interval values in the config file now fall back
  to their defaults with a warning, including `true`, fractional and infinite
  values, and the settings tool rejects such values for the integer settings it checks
  instead of reporting them applied.
