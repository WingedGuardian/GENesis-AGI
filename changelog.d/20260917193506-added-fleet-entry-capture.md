- SSH fleet connections now record the tmux pane-mode state of every slot to
  `~/.genesis/logs/fleet_entry_<date>.log`, so the intermittent "frozen session
  with a yellow line" (a slot left sitting in a `choose-tree` or `copy-mode`
  pane, which survives a disconnect) leaves evidence instead of vanishing when
  the window is closed. `grep ANOMALY ~/.genesis/logs/fleet_entry_*.log` after
  it happens. The capture only observes — it changes no pane and blocks no
  login — and the logs are owner-only and pruned after 45 days.
