- **Secrets-gate and provider-rule hardening.** Provider call patterns now
  match an explicit port (`api.openai.com:443`) and OpenAI `/v1/embeddings`.
  `rg x <(curl <endpoint>)` no longer slips the read-only exemption — input
  process substitution, a bare `&` background operator, and executor flags
  (`rg --pre`, `--hostname-bin`, `--pager`) all break the search exemption
  now. A quoted bare `secrets.env` that resolves in cwd no longer gates as
  if it were an operand (`allow_bare` honoured by the stat and inode arms),
  a quoted mention is not a denial, and a heredoc feeding an interpreter is
  scanned instead of stripped — `python3 <<EOF … cat secrets.env` can no
  longer hide a credential read. Globs deeper than two wildcard segments
  gate instead of walking past the hook budget. `behavioral_linter` and
  `secrets_env_access_guard` fail CLOSED when a shared sibling module cannot
  be imported (was exit 1 = non-blocking allow), a failed critical-
  observation write is logged to stderr instead of swallowed, and the hook
  write path uses the canonical guarded `connect_sqlite_rw` factory.
