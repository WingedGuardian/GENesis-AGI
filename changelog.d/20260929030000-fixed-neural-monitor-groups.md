- **The neural monitor maps every call site to a subsystem.**
  - 27 call sites that rendered only in an "Other" card are now grouped. Some
    join existing groups (memory, reflection, executor, learning, surplus);
    four new groups take the rest: Dream Cycle, Awareness, Conversation and
    Evaluation.
  - Sites without a hand-placed grid position now flow into rows below the
    map, instead of stacking on the top-left cell.
  - Four retired ids that only survived as stale run-history rows are marked
    removed, so they stop reappearing as live tiles.
  - A test now fails CI if any routing call site or live call-site metadata
    entry is left ungrouped, duplicated, or without a display name. Metadata
    entries that are not call sites of their own (an alias or a work tag) are
    marked `tile: False` and kept off the grid.
