- **A parser for the PR body's `## Acceptance` section and source pointer.**
  `scripts/acceptance_declaration.py` reads a PR body and reports what "done"
  means (the list items under the first `## Acceptance` heading) and where the
  work came from (`Closes #N`, a `Ledger:` or `Follow-up:` row id, or a
  `Spec:`/`Plan:` name). It reads a small, closed Markdown grammar over the same
  visibility scanner the other PR-body checks use, so comments and fenced
  examples never count. A body it cannot read reliably is refused with a reason
  rather than guessed at: one over GitHub's 65,536-character cap, or one with a
  code fence that is indented or inside a list item or quote. Nothing calls it
  yet; wiring it into PR creation and the merge report is a separate change.
