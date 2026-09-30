- **Re-applying Genesis gstack patches no longer replaces the current `codex`
  skill with an outdated copy.** `scripts/apply_gstack_patches.sh` used to copy
  a whole-file `codex` overlay captured against an old gstack release over the
  installed skill; it now leaves upstream's `codex` skill as shipped. This
  retires the old overlay's custom fallback chain — upstream's skill has no
  fallback when the Codex CLI is unavailable. The
  review-checklist overlay and the safety and description patches are unchanged.
