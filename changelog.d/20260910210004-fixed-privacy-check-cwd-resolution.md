- **The pre-push privacy check was quietly scanning nothing on a quarter of all
  pushes.** It looks at an outgoing push to the public repo for private data —
  install addresses, personal emails, local fingerprints — but it worked out which
  directory to look in by reading the command text, and it did that badly. A `~` was
  never expanded, so the everyday `cd ~/genesis/… && git push` resolved to a path
  with a literal `~` in it, which cannot exist. Every lookup against it failed and
  the check returned **no output at all** — identical to a clean result. Measured by
  replaying every real push in this install's command history through the old
  resolver and asking whether its answer could name a directory on any day:
  **195 of 708 (27.5%)** could not — **175 (24.7%)** from an unexpanded `~` and
  **20 (2.8%)** from an unexpanded variable or substitution. Every one of those
  scans silently produced nothing. Paths now expand, and where the target genuinely
  cannot be worked out — a variable, a subshell, an unquoted path with spaces — it
  says so instead of going silent. **93.8% of pushes now reach a concrete
  directory; 6.2% raise the notice; none resolve to an impossible path.** It still
  never blocks a push. A related case was worse than silence: `( cd elsewhere &&
  git push )` scoped its directory change inside a subshell, so the check scanned a
  **different repository** and reported that one clean.
- **A quoted `~` no longer sends that check to the wrong repository.** Bash expands
  a leading `~` only when it is unquoted, so `cd "~/wt"` enters a directory
  literally named `~/wt`. The check is handed an already-split token with the
  quoting gone, so it cannot tell the two apart — and expanding regardless meant
  that where a literal `~`-named directory also existed, it scanned a different
  real tree and reported *that* one clean. It now refuses when both readings name a
  real directory, which is the only case where the answer is genuinely ambiguous.
  Refusing every `~` instead would have cost a quarter of all pushes their scan to
  close a case measured at zero occurrences.
