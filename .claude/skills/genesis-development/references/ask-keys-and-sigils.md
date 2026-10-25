# Ask keys, blocked calls and `secrets.env`

- **A BLOCKED Bash call runs NOTHING** — including earlier `&&` segments and
  heredocs. If `mark && commit` is blocked, the `mark` did not happen either.
  Run gate-adjacent steps as separate calls.
- **A secrets.env access that did NOT prompt may have been silenced, or may
  have escaped the guard.** The guard matches only some access paths, so an
  unprompted access proves nothing by itself. An install may set
  `hooks.asks.secrets_env: off` in `~/.genesis/config/genesis.yaml`, which
  removes the credentials guard's prompt: the hook then emits no decision, only
  a context note naming the setting. That note is what proves a silenced
  access; with no note, assume the access went past the guard unseen. It
  approves nothing; other hooks and Claude Code's own permissions still decide
  the command, and a dispatched session is still denied. The other keys are
  `push_routine` (routine push prompts, 2026-10-06), `review_request` (a Codex
  review request whose review history could not be read; the round-cap prompt
  still asks, 2026-10-07) and `push_publish` (owner ruling 2026-10-01), all the
  same no-decision-plus-note shape; `hook_ask_policy.py` holds the exact scope
  of each. `push_publish` silences only the first push of the CURRENT branch, and only when the
  whole command is exactly one plain `git push` (e.g. `git push -u origin
  HEAD`), or that push followed by `&&` and one ready `gh pr create`. Two
  spellings also qualify for the lone push: `git -C <path> push …` when
  `<path>` is the literal, symlink-free top of a worktree of the SAME
  repository as the session's cwd, and a trailing `2>&1` and/or `| tail -N` /
  `| head -N`; the chained form allows neither. On the public repo only the
  chained form can be silenced, because of the first-push rule (build-session): a lone plain first
  push of the current branch there is denied, not silenced (other spellings
  keep their ask). Any other compound still asks. The remote git
  really pushes to must resolve (rewrites applied) to
  exactly `https://github.com/<public repo>` — ssh/scp forms always ask — with
  simple push config, no `http.*` config, no proxy/TLS/ssh/config env var in
  the hook's environment, and a live probe confirming the branch is absent
  there. Residue: shell-profile env/aliases/functions/PATH are invisible to the
  hook, and a concurrent process can still change config or publish the
  branch between check and push. `gh pr create` is not covered, since gh
  without a TTY aborts rather than pushing. Force pushes, other destinations,
  close-then-push, the no-open-PR block, round-cap asks and the dispatched deny
  are untouched, and any doubt about the destination keeps the prompt.
