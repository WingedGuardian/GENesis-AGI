- **The inbox now acts on its URL-coverage check, and tells you when it gives up
  on something.** An evaluation that never cites one of its input URLs as a
  `**Source:**` field is re-queued instead of being marked done. This check ran
  in observe-only mode until compliance was measured: across the first 42
  evaluations under the citation rule, every one cited its sources and none would
  have been re-queued. When an item uses up its retries, or a file trips the
  repeated-failure guard, you now get one alert per file naming each item it
  stopped on. Previously that
  item simply stopped being evaluated, with no signal. The retry lane of that
  guard now parks the file like the other two paths instead of re-checking it on
  every scan. A `**Source:**` line inside a list item or blockquote now counts as
  a citation. With network parking live (`resilience.parking_mode: live`),
  an outage no longer spends an item's retries. Invalid
  `items_per_eval`, `max_retries`, `timeout_s` and interval values now fall back
  to their defaults with a warning instead of being accepted. To observe without
  acting, set `url_coverage_mode: shadow`.
