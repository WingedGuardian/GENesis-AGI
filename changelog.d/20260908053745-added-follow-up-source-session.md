- **Follow-ups now record which session they came from.** The `source_session`
  column existed and repo-pulse read it to attribute completions — but no live
  producer wrote it: across the four sources that create follow-ups today, 513
  of 513 rows were NULL, so every annotation the repo-pulse worker made carried
  empty provenance. It now fills without guessing. The inbox evaluator and the
  task executor pass through the session id they already held and were
  dropping. The `follow_up_create` tool takes a `source_session` the calling
  session reads off its own per-turn tag: an 8-character prefix is resolved
  against every store that can hold a session id — including the one written
  before a new session has done anything else, which is exactly when a session
  first describes its own work — and a prefix that does not resolve to a single
  complete id is stored as NULL, and says so, rather than as a truncated or
  malformed one. A dispatched session, which never receives that per-turn tag
  and so had no way to know its own id, is now told it at session start.
  Callers with no session, like surplus ideation, keep an honest NULL.
