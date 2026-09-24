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
  (`CC_TMP_CAPACITY_MB` in watchgod.conf, default 2048), deliberately without
  reference to how full the underlying disk is: that disk is shared on every
  supported setup, so counting it would have let a full system disk trigger the
  emergency inside an almost-empty temp directory and destroy in-flight work
  without freeing anything. A genuinely full disk keeps its own separate
  trigger. Below 150 MB of true
  headroom every protection is bypassed and everything reclaimable is
  reclaimed — including files written seconds ago and the active session's own
  temp tree, with unix sockets the one exception because deleting them frees
  nothing and breaks cross-session messaging. That exception now actually
  holds everywhere: the cache cleanup used to delete sockets sitting inside a
  cache directory moments after the main pass had deliberately kept them.
  Nothing may stand between Claude Code and its temp space. The emergency is also now evaluated when deciding which cleanup tier
  to run, not only inside the most aggressive one, so a temp budget configured
  larger than the volume can no longer keep it out of reach. Two further
  cleanup defects are fixed alongside: the check that confirms the
  in-use detection is working was itself misreporting failure on any
  real-sized scan, and the cleanup silently reclaimed nothing at all whenever
  no Claude Code session directory was present.
