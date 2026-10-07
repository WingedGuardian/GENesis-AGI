- A force push away from origin is now blocked unless the whole command is one
  plain `git push`, with no prefix, wrapper, git global option, redirect, pipe
  or other step; its destination is read before the command runs, so those
  additions can change what the push targets. Non-force pushes carrying any
  prefix, wrapper or untrusted git global config option are blocked regardless
  of destination, because supplied config can retarget the push or add a forced
  refspec such as `remote.<name>.push=+…` or
  `remote.<name>.pushurl=…` that has no force flag. A force push directly to
  origin keeps the specific public-repository block.

  The structural force-push rule deliberately means retyping commands such as
  `git -C <dir> push -f backups`, `git push -f backups 2>&1`,
  `git push -f backups | cat`, `cd x && git push -f backups` and
  `timeout … git push -f backups` as a single plain push. For non-force pushes,
  `git -C <dir>`, `git -P` and `git --no-pager` remain safe; command prefixes
  and wrappers are blocked even for a disjoint remote.
