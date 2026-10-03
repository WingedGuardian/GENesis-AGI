- **A parser for the PR body's `## Acceptance` section and source pointer.**
  `scripts/acceptance_declaration.py` reads a PR body and reports whether it
  declares what "done" means: the first `## Acceptance` heading outside fenced
  code, its list items, and the work's origin (`Closes #N`, a `Ledger:` or
  `Follow-up:` row id, or a `Spec:`/`Plan:` name). A body over GitHub's 65,536
  character cap is refused whole rather than truncated, so a section past the
  bound can never report as absent. Pure and stdlib-only, sibling to
  `scripts/e2e_declaration.py`; wiring it into the merge gate and
  `gh pr create` is maintainer follow-up.
