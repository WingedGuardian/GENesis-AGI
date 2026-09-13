- **Filing a bug you find while reviewing no longer needs to be asked about.** The
  rule used to require explicit approval for every GitHub issue, which is what kept
  adjacent bugs unfiled — someone finds a real problem mid-review, has nowhere cheap
  to put it, and it evaporates. Now the gate is what goes IN the issue rather than
  permission to open one: scrub anything personal or identifying, keep it to
  technical detail, and ask when a case is borderline either way. A security defect
  is still never filed publicly before it is fixed.
- **Three review-process rules that said two things at once now say one.** The docs
  claimed a gate-touching PR gets no stale-review leniency at all, while a passage
  further down measured the opposite — the check is on the delta's files, not the
  PR's, so a docs-only follow-up can still be trivial. The code agrees with the
  second. And a usage-limits reply from the code reviewer was described as proof it
  cannot review, contradicting a measured case where findings arrived anyway; it is
  now the strongest available evidence rather than proof, which matters because that
  judgement is what authorises reaching for a fallback reviewer.
