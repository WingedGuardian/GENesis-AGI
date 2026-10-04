- A force push whose command carries its own git config — a `git -c` /
  `--config-env` option, a `HOME=` or `GIT_CONFIG_*` prefix, or an earlier
  segment that can write config — can no longer downgrade the push guard's
  destination check from a block to an ask. The disjoint push-URL read now
  applies only when the whole command passes the allowlist the re-push
  relaxation already uses; anything else is treated as public and blocked.
