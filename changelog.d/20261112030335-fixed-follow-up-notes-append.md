- **`follow_up_update` no longer erases a follow-up's earlier resolution notes.**
  The tool documents `resolution_notes` as appended context for future sessions,
  but every update replaced the column, so a one-line ruling wiped the scope
  notes and cross-links earlier sessions had left. New notes are now appended
  after a blank line on every path (notes only, with a status, with a blocked
  reason, and for validators), the same way PR evidence is already appended.
