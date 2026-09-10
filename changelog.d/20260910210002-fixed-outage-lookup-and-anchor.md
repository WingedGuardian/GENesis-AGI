- **Provider-outage notifications are immune to a crowded observations table,
  and a stale incident can no longer inflate a new outage's duration.** The
  outage clock is now read with a hash-scoped, index-covered query instead of
  a fetch-100-and-filter pass — on an install with more than 100 unresolved
  routing observations, the old read silently dropped the target provider's
  row and reported no outage at all. Separately, a provider that flaps for
  weeks (never fully recovering) kept its original in-memory anchor forever;
  trips more than 24 hours apart now start a fresh incident window, so an
  escalation reports the real incident's age instead of a weeks-old one.
