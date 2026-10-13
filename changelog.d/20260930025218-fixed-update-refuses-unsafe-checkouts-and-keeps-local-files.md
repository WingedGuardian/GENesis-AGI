- **`update.sh` refuses a checkout it must not deploy, before it touches anything.**
  A linked worktree at any path (git decides, not the path name), a bare
  repository, a detached HEAD, or any branch other than `main` is refused before the
  lock, the rollback tag and the backup, and the message names the branch it found.
  An update run on another branch used to merge `main` into that branch and restart
  services on the result. `GENESIS_ALLOW_NON_DEPLOY_BRANCH=1` admits another named
  branch deliberately; it never admits a worktree, a detached HEAD, or `live`. The
  checks now live in `scripts/lib/deploy_checkout.sh`, which
  `scripts/deploy_code_only.sh` also uses (with no override), so the two deploy
  paths cannot disagree. `update.sh`'s dirty-tree check is the shared one too: a
  file renamed into an excused path now counts as a local change, and an unreadable
  working-tree status refuses instead of passing as clean.
- **`update.sh` deploys the commit it fetched.** It pins the fetched `main` head and
  merges that exact commit; a fetch by another session in the same checkout can no
  longer change what is deployed. A merge that reports success without bringing
  that commit in, or that landed on a branch other than the one the run started
  on, fails the update.
- **`update.sh` never resets a checkout someone else is using.** If another session
  or editor switches the branch, commits, or edits a tracked file while an update
  runs, the update refuses before it merges. During the pre-update backup nothing
  has stopped yet; after the stop, the rollback takes over. That rollback resets
  only the merge it made itself. A plain branch switch with nothing uncommitted is
  switched back without force, and the other branch keeps its commits. Any other
  move is left exactly as it is. Services are reinstalled and restarted only on the
  pre-update code; otherwise the rollback reports itself incomplete and says what
  is in the way. The old rollback checked the original branch out and hard-reset
  it, which could destroy that work, and could restart services on code nobody
  had validated.
- **`update.sh` no longer overwrites a local ignored file that the update starts
  tracking.** Before any service stops, it lists the files the incoming commits
  add or change and refuses, naming them, if any already exist locally as untracked or
  ignored files (a local settings or secrets file, for example). Move them aside and
  re-run. The same check runs again as the last step before the merge, so a file
  that appears while services are stopped is refused too, before anything merges.
- **Local edits to `AGENTS.md` and `config/procedure_triggers.yaml` are kept.**
  `update.sh` used to discard them before every merge, with no copy. It now saves
  them first, before any service stops, under `~/.genesis/premerge-backups/<run>/`
  (a patch plus a copy) and prints where. `update.sh`'s own rollback, which resets
  tracked files, first saves any edit not already backed up, including on a
  `--post-merge` run. It discards an edit only when the update changes
  that file or the edit is staged (otherwise git keeps it through the merge), and
  only once a backup of its current content exists. The daily disk-hygiene run prunes these backups
  after 45 days.
