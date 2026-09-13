- **A dead code index now says so.** The code indexer could give up on a
  repository permanently and report it only into a log file: on one install the
  main repo's index request was abandoned after five failed attempts, its
  database sat corrupt for two weeks, and code-intelligence tools kept answering
  from nothing — with no alert anywhere, because nothing read the indexer's
  failure records. An hourly check now reads those records directly (abandoned
  index requests, and whether an index actually exists for the path being
  indexed) and raises one self-resolving alert that clears when the index comes
  back. It reads the indexer's own files rather than asking the tool how it is
  doing, since a crashed indexer cannot answer.

  It stays quiet while an index is merely being waited for. A fresh install
  queues its first index request at setup and the builder only runs when the
  machine is idle, so "requested but not yet built" is the normal state of a
  healthy new clone for its first hours — the check speaks only once the request
  has been abandoned outright, or has waited past the point where the builder
  stops holding back. It also stops reporting a past failure once a later
  attempt has succeeded.

  Configurable via `code_intel_health` (`enabled`, `indexed_path`,
  `alert_priority`). Leave `indexed_path` empty unless you have also re-pointed
  the indexer itself: setting it alone only moves what this check looks for.
