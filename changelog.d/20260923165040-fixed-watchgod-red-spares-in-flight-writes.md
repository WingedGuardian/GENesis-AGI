- The temp watchdog no longer deletes files a program is still writing, and its
  emergency trigger now measures the real ceiling. Under temp pressure the
  cleanup removed directories without checking whether anything was using them,
  so a long download or install could die mid-write on its own missing files;
  cleanup now leaves a directory alone while a process holds a file open inside
  it (writers that close each file as they go are not covered — the check sees
  only descriptors open at sweep time). Separately, on btrfs-backed installs
  the "disk nearly full" emergency check was reading the shared pool's free
  space instead of the cc-tmp volume's true 2 GiB cap and so could never fire;
  it now computes headroom against the configured capacity
  (`CC_TMP_CAPACITY_MB` in watchgod.conf, default 2048), and below 150 MB of
  true headroom every protection is bypassed and everything reclaimable is
  reclaimed — nothing may stand between Claude Code and its temp space.
