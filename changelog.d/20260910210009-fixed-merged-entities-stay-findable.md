- **Merged entities stay findable, and merge chains stay walkable.** Four
  rails for the moment entity merges start applying: asking about something by
  a name that was merged away now finds the surviving entity instead of
  nothing; lookups follow multi-hop merge chains to the live record instead of
  stopping one hop in at a tombstone (and each new merge compacts existing
  chains as it lands); a person or organization sharing a name with a concept
  can no longer block the concept lookup and mint an avoidable duplicate; and
  a merge proposal invalidated by identity drift re-enters the review queue
  immediately instead of waiting for the weekly sweep to rediscover it.
  Verdicts also now record which judging policy produced them, and pairs
  judged "distinct" under the old, stricter policy are re-examined under the
  current one — previously they were settled forever on rules that no longer
  apply.
