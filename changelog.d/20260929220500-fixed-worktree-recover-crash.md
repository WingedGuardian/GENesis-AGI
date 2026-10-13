- **`worktree_lifecycle.py --recover` works again.** Two changes that each
  passed on their own met on `main`: `--recover` gained a `--dry-run` preview,
  and recovery moved into a locked helper that was never passed the dry-run flag.
  Every recovery, preview or real, then stopped with `NameError` before
  touching anything. The flag is now passed through, so an archived worktree can
  be previewed and restored again.
