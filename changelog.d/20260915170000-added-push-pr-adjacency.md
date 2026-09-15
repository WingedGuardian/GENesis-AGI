- **The push gate now enforces the publish rule it could only document.** A
  prompt, not a block. A re-push to a branch that is public but has no open
  pull request no longer slides through on its first-push approval: the gate
  asks, and says why — such a branch runs no CI and no leak scan at all, which
  is the state the publish rule exists to prevent. The lookup fails toward the
  previous behaviour: a question it cannot answer changes no verdict and
  manufactures no prompt, and a dry run stays silent because it publishes
  nothing.
