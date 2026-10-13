- **Merged entities stay findable, and merge chains stay walkable.** Four
  rails for the moment entity merges start applying: asking about something by
  a name that was merged away now finds the surviving entity instead of
  nothing; lookups follow multi-hop merge chains to the live record instead of
  stopping one hop in at a tombstone; a person or organization sharing a name with a concept
  can no longer block the concept lookup and mint an avoidable duplicate; and
  a merge proposal invalidated by identity drift re-enters the review queue
  immediately instead of waiting for the weekly sweep to rediscover it.
  Verdicts also now record which judging policy produced them, and pairs
  judged "distinct" under the old, stricter policy are re-examined under the
  current one — previously they were settled forever on rules that no longer
  apply. The `live` entity-adjudication mode, which merges new pairs with no
  approval step, is turned off for now: it runs as `propose_only` and the
  settings lever refuses it until a check at merge time lands (#2742).
  Approved merges still apply through `entity_adjudication_apply`.
