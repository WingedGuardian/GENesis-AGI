- **`follow_up_update` can correct a note: `replace_notes=True`.** Notes are
  still appended by default. Passing `replace_notes=True` with
  `resolution_notes` replaces the row's notes instead, for fixing a wrong or
  sensitive note. It is refused without replacement text, and validators can
  only append.
