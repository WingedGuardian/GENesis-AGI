- A force push away from origin is now blocked unless the whole command is one
  plain `git push`, with no prefix, wrapper, git global option, redirect, pipe
  or other step; its destination is read before the command runs, so those
  additions can change what the push targets. Non-force pushes to origin/public
  are also blocked when the push carries a prefix, wrapper or untrusted git
  global config option, which could supply a forced refspec such as
  `remote.origin.push=+…` that has no force flag. A force push directly to
  origin keeps the specific public-repository block.

  The structural force-push rule deliberately means retyping commands such as
  `git -C <dir> push -f backups`, `git push -f backups 2>&1`,
  `git push -f backups | cat`, `cd x && git push -f backups` and
  `timeout … git push -f backups` as a single plain push. For non-force pushes,
  `git -C <dir>` remains safe; command prefixes and wrappers to origin do not.
