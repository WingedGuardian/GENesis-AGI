- **The neural monitor maps every call site to a subsystem.**
  - 26 call sites that rendered only in an "Other" card are now grouped. Some
    join existing groups (memory, reflection, executor, learning);
    four new groups take the rest: Dream Cycle, Awareness, Conversation and
    Evaluation.
  - Every group stays in one connected block on the grid. The hand-placed
    map gives each of its groups exactly one cell per site, and the new
    groups flow into rows below it, each starting on a new row when it would
    otherwise wrap. Before, a site without a position stacked on the
    top-left cell.
  - Five ids that only survive as stale run-history rows are marked removed,
    so they stop reappearing as live tiles: four with no caller or a newer
    name, and one job that now runs as a direct session and records no
    call-site data.
  - A test now fails CI if any routing call site or live call-site metadata
    entry is left ungrouped, duplicated, or without a display name, or if a
    hand-placed group has the wrong number of cells or a split block. Metadata
    entries that are not call sites of their own (an alias or a work tag) are
    marked `tile: False` and kept off the grid.
