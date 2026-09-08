- **A Codex round request whose PR number is not written literally is now
  refused instead of skipping the round cap.** The cap counts a PR's existing
  Codex reviews and blocks a further round until a conscious
  `# escalation-ack`, but it resolves the PR from the command text — and a
  `PreToolUse` hook sees that text *before* the shell expands anything. A target
  like `$n`, `"$PR"` or `$(…)` therefore resolved to nothing, and the gate
  skipped that segment entirely rather than refusing it.

  Measured: writing the request as
  `for n in 1625 1576 1609; do gh pr comment $n --body "@codex review"; done`
  posted round requests against two pull requests already at or past the cap,
  with no acknowledgement and none of the step-back triage the block exists to
  force. The same request written with a literal number was refused correctly,
  so the cap was doing its job whenever it could tell which PR it was reading.

  Only an *unexpanded expansion* fails closed. A missing target and a literal
  branch name keep their documented fail-open: both are knowable in principle,
  whereas an expansion's value does not exist yet at hook time. The
  acknowledgement sigil deliberately does not clear this refusal either — it is
  computed across the whole command, so one sigil on a loop would otherwise
  license a round on every pull request the loop touched.
