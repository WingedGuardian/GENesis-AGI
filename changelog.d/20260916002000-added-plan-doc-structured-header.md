- **Plan documents carry a structured header.** A plan that outlives one
  session now opens with YAML frontmatter naming its status, the `origin/main`
  it was written against, what adopting it commits the project to (`binds`),
  what it forecloses (`prevents`), and the decision/ledger/issue ids it
  executes — plus a `## ═══ SUPERSEDED BELOW ═══` divider separating live
  content from archaeology. Three failure modes motivate it: three things
  enumerate the plan directory but all key on filename and mtime, so a
  commitment living only in a plan file is tracked by nothing; a long-running
  plan accretes with no signal for which part is current (measured on one
  install: 2,501 of 4,488 lines below the divider); and the line numbers and
  PR heads a plan cites go quietly false. `pinned.main` turns the last one
  into a one-command read. The header is written by hand and nothing validates
  it yet — the upstream linter it borrows vocabulary from checks body
  structures this header does not emit, so a checker is a separate later
  change.
