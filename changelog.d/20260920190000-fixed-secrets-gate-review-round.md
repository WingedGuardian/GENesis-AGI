- **Secrets-gate and provider-rule hardening.** Provider call patterns now
  match an explicit port (`api.openai.com:443`) and OpenAI `/v1/embeddings`.
  `rg x <(curl <endpoint>)` no longer slips the read-only exemption — input
  process substitution, a bare `&` background operator, and executor flags
  (`rg --pre`, `--hostname-bin`, `--pager`) all break the search exemption
  now, and that executor test is SHARED with the `git` branch, which had been
  returning read-only for any `git log|grep|show|diff|blame` regardless of
  its flags. Neither half of that flag has a fixed spelling, so the test is a
  per-subcommand regex rather than a literal table: git accepts any
  unambiguous prefix (`--op=<cmd>` runs), and `-O` bundles into any short
  cluster (`-nO<cmd>` runs). The command is tokenized with `shlex` instead of
  `.split()` because the guard reads it as typed while the shell hands the
  tool a de-quoted argv — `rg "--pre" <cmd>` slipped every table on the quote
  alone. `-O` is scoped per subcommand: on `grep` it is that pager flag, on
  `diff`/`show` it names an ORDER FILE, so matching it everywhere would swap
  one fail-open for a fail-closed. A quoted bare `secrets.env` that resolves
  in cwd no longer gates as
  if it were an operand (`allow_bare` honoured by the stat and inode arms),
  a quoted mention is not a denial, and a heredoc feeding an interpreter is
  scanned instead of stripped — `python3 <<EOF … cat secrets.env` can no
  longer hide a credential read. Globs deeper than two wildcard segments
  gate instead of walking past the hook budget. `behavioral_linter` and
  `secrets_env_access_guard` fail CLOSED when a shared sibling module cannot
  be imported (was exit 1 = non-blocking allow), a failed critical-
  observation write is logged to stderr instead of swallowed, and the hook
  write path uses the canonical guarded `connect_sqlite_rw` factory.
  Wiring `NotebookEdit` read every cell as executable, so a markdown cell
  that merely documented a provider endpoint was hard-blocked by the same
  rule that exempts `*.md` for that exact content — a notebook's path ends
  in `.ipynb`, so the documentation globs could never see the cell. Only an
  explicitly prose cell type is exempt; an absent or unrecognised one is
  still read as code. Both guards' docstrings now state the measured scope
  (3 of 11 access paths, issue #2230) — the correction had landed in one of
  the two copies, leaving the public guard still telling readers the gap was
  two known residuals.
