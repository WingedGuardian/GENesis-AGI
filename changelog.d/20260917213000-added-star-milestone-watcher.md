- **A daily watcher that announces once when the public repo crosses a star
  milestone**, so work parked behind "revisit at N stars" wakes up on its own
  instead of waiting to be remembered. It reads the count unauthenticated, writes
  a permanent observation on the transition, and stays silent otherwise; an
  install with no repo configured falls back to the checkout's own `origin`, and
  one with neither is a clean no-op rather than a daily failing unit.
