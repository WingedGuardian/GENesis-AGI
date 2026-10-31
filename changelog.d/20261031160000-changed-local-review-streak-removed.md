- **The local review streak is gone.** `scripts/review_state.py` no longer keeps
  a per-worktree round count, the statusline drops its `streak:` field (it now
  reads `<branch> · ledger:<n> · PR#<n>:<state>`), and the dormant cap menu on
  `AskUserQuestion` is unwired. `review_state.py mark --source external
  --defects|--clean` is still accepted and records the reviewer and its outcome
  in the review marker for provenance; no gate reads either. Review rounds are
  counted only from GitHub (`scripts/review_budget.py`). Old
  `~/.genesis/review_rounds/` files are no longer read and can be deleted.
