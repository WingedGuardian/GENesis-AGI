- **A Codex round request is now refused unless the hook can read which pull
  request it names.** The cap counts a PR's existing Codex reviews and blocks a
  further round until a conscious `# escalation-ack`, but it resolves the PR
  from the command text — and a `PreToolUse` hook sees that text *before* the
  shell expands anything. Any identity it could not read simply skipped the cap.

  Measured: writing the request as
  `for n in 1625 1576 1609; do gh pr comment $n --body "@codex review"; done`
  posted round requests against two pull requests already at or past the cap,
  with no acknowledgement and none of the step-back triage the block exists to
  force. The same request written with a literal number was refused correctly,
  so the cap was doing its job whenever it could tell which PR it was reading.

  Both identity-bearing values are now covered — the PR target *and* the
  repository (`--repo`/`-R`, or the owner/repo inside a URL target); with a
  literal number and an unreadable repo the count went to a nonexistent API
  path, errored, and failed open. Each is accepted by an **allowlist** of what
  gh documents (`[<number> | <url> | <branch>]`, `[HOST/]OWNER/REPO`), spelled
  in characters the shell does not act on, so a spelling nobody thought of is
  refused by construction rather than added to a list next time.

  What this costs, since it will be met in normal use: the common
  `gh pr comment 1234 --repo "$SLUG" --body "@codex review"` now needs the slug
  written out (or dropped, when running inside the repo). Measured over 530
  distinct real round requests, 47 (8.9%) are refused; in that corpus every one
  of them is a request whose PR or repository the cap genuinely could not read.
  A PR URL keeps working as copied from a browser, `/files`, `#issuecomment-…`
  and all.

  Two fail-opens are deliberately kept: a request with no positional target at
  all, and a literal branch name. The acknowledgement sigil does not clear this
  refusal either — it is computed across the whole command, so one sigil on a
  loop would license a round on every pull request the loop touched.
