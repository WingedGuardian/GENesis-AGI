- **`ego_decision` and proposal-rejection capture no longer fold a new ruling into
  an existing decision that only shares its `[type/category]` tag.** Tags match
  literally, only a repeat of the same ruling reaffirms, and `record` returns
  the other same-tag rulings as `related` with `related_total`.
