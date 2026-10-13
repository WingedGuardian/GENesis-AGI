- **The commit review gate no longer asks for approval on commits in unrelated
  repositories.** A commit in a scratch repository with no remote (or only a
  filesystem remote) asked "the open pull request's review history could not be
  read reliably", and was refused outright in a background session, because the
  failed pull-request lookup was read as unreadable evidence rather than as
  "gh has no repository to look in here". The review budget now applies only to
  an open pull request on the configured public repository. A failed lookup
  consults no budget when the repository has nothing gh could resolve to GitHub
  (no remote or branch push target that might be GitHub — a target naming a
  remote counts by that remote's own URLs, so a clone of a local repository
  qualifies — and `GH_REPO` and `GIT_DIR` unset) and the command is a single
  `git commit`, optionally with `-c <key>=<value>` and one
  `-C <dir>` — the one shape that provably commits in the repository that was
  checked. A branch whose open pull request lives in a repository with a
  different name also consults no budget. Nothing changes on the public
  repository itself: an unreadable lookup there still asks in the foreground and
  is refused when dispatched. Every other shape keeps that behaviour too,
  including `cd <dir> && git commit` (use `git -C <dir> commit` in a scratch
  repository), `--git-dir`, `GIT_DIR`, `pushd`, `env -C`, a wrapper, process
  substitution, a commit message built with `$(…)`, a remote that might be
  GitHub, or an
  undeterminable public repository.
