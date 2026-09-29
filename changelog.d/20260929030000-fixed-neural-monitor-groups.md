- **The neural monitor maps every call site to a subsystem.**
  - 26 call sites that rendered only in an "Other" card are now grouped. Some
    join existing groups (memory, reflection, executor, learning);
    four new groups take the rest: Dream Cycle, Awareness, Conversation and
    Evaluation.
  - Sites without a hand-placed grid position now flow into rows below the
    map, instead of stacking on the top-left cell.
  - Five ids that only survive as stale run-history rows are marked removed,
    so they stop reappearing as live tiles: four with no caller or a newer
    name, and one job that now runs as a direct session and records no
    call-site data.
  - A test now fails CI if any routing call site or live call-site metadata
    entry is left ungrouped, duplicated, or without a display name. Metadata
    entries that are not call sites of their own (an alias or a work tag) are
    marked `tile: False` and kept off the grid.
