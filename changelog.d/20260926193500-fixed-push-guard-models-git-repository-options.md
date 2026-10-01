- **The push and merge guards now account for git's repository-selection
  options.** Besides the working directory and `git -C`, git can be pointed at a
  repository with `--git-dir` / `--work-tree` or the `GIT_DIR`, `GIT_WORK_TREE`
  and `GIT_COMMON_DIR` environment variables. The guards resolved the branch,
  remote and push configuration from the directory a command ran in, so a
  command using those options was classified against the wrong checkout. They
  now treat any of these forms as a working directory they cannot resolve, which
  every check already handles conservatively: a force push is refused, a publish
  asks for approval instead of qualifying as a routine re-push, and a merge onto
  the default branch is refused. The pre-push privacy review now reports that it
  did not scan such a push, rather than scanning the diff of the directory the
  command ran in. Pushing or merging from inside the repository (`cd` or
  `git -C`) behaves exactly as before.
