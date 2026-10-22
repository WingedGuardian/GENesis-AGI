- **The open review round is found by the head, not by list position.** A PR's
  rounds are listed in the order of its commit list, which GitHub sorts by date,
  so after a rebase or cherry-pick a commit can be listed after the head. The
  budget then read the head's open round as complete, and its findings as owed
  by nobody. `review_budget`, `review_reflection` and the `--check-pr` rounds row
  now find the open round as the one whose head is the current head; a budget
  that says a round is open but carries no single entry for the head owes an
  unknown amount, never nothing. Every open PR owes the same as before; the
  `status` output's `round_head` is now empty when no round is open, where it
  used to name the newest round by list position. The round trend still reads
  list order and is fixed separately.
