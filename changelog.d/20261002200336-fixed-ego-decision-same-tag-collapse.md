- **`ego_decision` and proposal-rejection capture no longer fold a new ruling into
  an existing decision that only shares its `[type/category]` tag.** Tags match
  literally, only a repeat of the same ruling reaffirms (whichever path recorded
  it first, and even when the 500-character cap cut a rejection's provenance
  note), and `record` returns the other same-tag rulings, most recently affirmed
  first, as `related` with `related_total`.
