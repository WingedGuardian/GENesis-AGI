- **Disk reclaim no longer deletes the code-intel indexes on its own.**
  `scripts/disk_reclaim.py` used to clear the code-intel index databases at
  95 % disk use whenever a caller did not say otherwise, and the automatic
  disk-cleanup remediation was one such caller. Deleting them forces a full
  re-index later, which is heavy. The script now never clears them unless a
  threshold is passed with `--last-resort-above`, and only the disk guardian's
  last-resort pass does that. Package and download caches are still cleared
  as before.
