- **The push gate now enforces the publish rule it could only document.** Two
  additions, both prompts rather than blocks. A re-push to a branch that is
  public but has no open pull request no longer slides through on its
  first-push approval: the gate asks, and says why — such a branch runs no CI
  and no leak scan at all, which is the state the publish rule exists to
  prevent. And the first push of a branch whose tip commit already belongs to
  an existing pull request names that request in the approval prompt, because
  publishing the same work under a second branch name is exactly how this
  repository grew duplicate pull requests for changes that had already merged —
  a squash merge severs ancestry, so once the first name merges, nothing else
  can tell the second name is the same work. Both lookups fail toward the
  previous behaviour: an unanswerable question changes no verdict and
  manufactures no prompt.
