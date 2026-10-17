- **Refuse unsupported pinned Codebase paths before staging.** Linux repository
  and cache identities containing a backslash, including symlink targets, now
  refuse before native configuration can rewrite the path and select another
  cache. Status retains metadata and independently checks systemd, including
  when a provider path contains a symlink loop. Literal binary/interpreter paths
  and structural reads of old settings retain their existing behavior.
