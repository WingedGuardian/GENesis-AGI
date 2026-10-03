- **Eval runs keep their lock files in `~/.genesis/locks/`, not `~/tmp`.**
  The bench, gauntlet and skill-replay runners each refuse to start while
  another run of the same kind is going. Their lock files lived in `~/tmp`,
  scratch space that is pruned by age and can fill up, so a lock file pruned
  between runs could fail to be re-created and stop a run from starting. For
  one release a run also takes the old `~/tmp` lock, so a runner from before
  the update and one from after it still cannot run at the same time.
