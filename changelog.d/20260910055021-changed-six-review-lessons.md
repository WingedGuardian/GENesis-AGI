- **The development guide now covers five ways a verification can be complete
  and still be wrong.** All five came out of one long pass through the pull
  request queue, and each had already cost hours. A change can be verified three
  separate ways and still ship a resource defect, because a correctness suite
  cannot go red on one — and where the code sits on a path that gets killed on a
  timeout, and the kill permits what it was meant to refuse, a scan that slows
  down super-linearly on attacker-chosen input is the defect no matter how right
  its answers are; the fix is a timing test. A clean measurement over a corpus of
  real inputs is a lead and not a clearance, because a corpus records what has
  been typed rather than what can be typed, so a null result now has to be handed
  on with its denominator and its blind spot attached. The matrix that replaces
  it is generated from the grammar of what a rule flags rather than from history,
  run against both trees so the baseline can be seen to move, with a note on the
  probe that reads a verdict from an exit code and so cannot tell "ask" from
  "allow". Refuting a reviewer's finding is itself a claim at the same evidence
  bar, and an admitted gap in the argument is the reason not to state the
  conclusion. And a local checker's verdict about a remote pull request depends
  on the tree it ran in, so a claim about a PR's current state has to come from a
  tree at the default branch. Recorded alongside the first of those: a lesson
  about a vulnerability does not need the vulnerability's parameters — the shape
  of the failure is what teaches, while exact inputs, thresholds and affected
  counts only compose into a recipe that keeps working for anyone still on the
  unfixed code.

- **The guide now says which pull request to pick up first when several are
  eligible.** A fix restores functionality that is already broken, so it goes
  ahead of new functionality — but the measurement behind that is about size
  rather than kind. Across the 65 open fix and feature pull requests on this
  repository, features average 2,024 added lines against 804 for fixes, and 67%
  of them exceed a thousand lines against 29%; separately, 86% of pull requests
  over a thousand lines need three or more external review rounds, against 6%
  under two hundred. The gap in findings per pull request is much smaller, so the
  rule is written on the property rather than the prefix: a small feature is not
  deprioritised, and a large fix gets no pass. It remains one input among
  several, not an ordering.
