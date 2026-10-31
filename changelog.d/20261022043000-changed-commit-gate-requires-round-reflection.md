- **The commit gate now requires a round reflection before a review round's fixes.**
  While a PR's open review round owes a reflection (what
  `python3 scripts/review_reflection.py status` prints), a content commit is
  denied, in foreground and dispatched sessions alike. The deny names the owed
  keys and the round's obligations (a class audit or a premise check, by lane
  and round). The session writes the reflection, checks it with
  `review_reflection.py validate`, and commits it alone with
  `git commit --allow-empty --cleanup=verbatim -F <file>`; then the fix commits.
  An unreadable review budget keeps the existing path (the round-approval ask,
  or a deny when dispatched); anything else the gate cannot read (the owed keys,
  git, or time) allows the commit with an advisory. An install can set
  `hooks.reflection_gate: advisory` to see the verdicts without enforcing them.
- **The local review streak's two tiers are retired.** The round-2 mode-switch
  block and the round-3 hard stop no longer exist, and `# audit-ack` and
  `# escalation-ack` authorize nothing.
- **Late review findings are owed from the gate's go-live.** Reviews that arrive
  after a round's fix (`review_late`) now count toward what a reflection must
  answer, from the moment the gate started enforcing; PRs that were merge-ready
  at that moment owe nothing for rounds they had already opened.
