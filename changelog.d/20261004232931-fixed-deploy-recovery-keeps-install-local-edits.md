- **Bootstrap stops when crash recovery cannot be safely completed.**
  Crash recovery no longer runs `git reset --hard`: it undoes only its own
  update's merge with a non-forced checkout. If ownership or a safe rollback
  cannot be established, bootstrap exits 1 and keeps the recovery state file.
  If a merge is in progress, bootstrap refuses without aborting it — run
  `git merge --abort` yourself, then rerun bootstrap.
