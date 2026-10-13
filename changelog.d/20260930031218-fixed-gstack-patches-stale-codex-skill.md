- **Re-applying Genesis gstack patches no longer replaces the current `codex`
  skill with an outdated copy.** Genesis no longer ships a `codex` overlay;
  `scripts/apply_gstack_patches.sh` now leaves the upstream gstack `codex` skill
  as-is. An install that customises its `codex` skill maintains that
  customisation locally. The review-checklist overlay and the safety and
  description patches are unchanged.
