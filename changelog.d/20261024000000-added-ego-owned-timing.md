- Added a separate offline C1 actual-clock/lifecycle instrument for six timer
  wakes, three fixed reactive arrival offsets, SIGTERM during backoff and writer
  contention. Actual contained evidence and independent readback are required;
  this adds no production runtime or Codex execution path.
