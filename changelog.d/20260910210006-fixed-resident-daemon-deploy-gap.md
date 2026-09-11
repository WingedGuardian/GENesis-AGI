- **Merged changes to resident daemons now actually deploy.** A systemd user
  unit that stays resident (like the temp-protection watchgod) only re-executes
  its backing script when something restarts it — and nothing did: not
  `update.sh`, not bootstrap, so a merged fix could run weeks late while the
  unit dutifully kept its pre-merge copy alive. `update.sh` now compares each
  resident repo-script daemon's start time against its script's on-disk change
  time and restarts the stale ones, on no-op runs too (a daemon left stale by
  an earlier pull must not stay stale just because today's merge brought
  nothing). The deploy-health snapshot gains the matching `stale_units`
  finding, so a daemon running pre-update code raises the standing
  deploy-drift alert between updates instead of staying silent. The installer
  also stops overwriting the rendered watchgod unit with a legacy copy that
  hardcoded the repo path.
