- **Code-only deploys save install-local edits before resetting ephemeral files.**
  They store edits under `~/.genesis/premerge-backups`, refresh backups after
  service stop hooks run, and refuse flagged edits that cannot be safely reset.
  A failed reset is not reported as completed and still raises the deploy-failure alert.
