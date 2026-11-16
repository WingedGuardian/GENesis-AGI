- **The Guardian's RAM alert pages a tier change once instead of on every
  crossing.** A reading hovering at a tier line, a dip and return to a tier
  already paged, and a one-tick failed container read each used to send a page
  (on one live day, 30 of the Guardian's 32 messages). A tier is now left only
  `memory_tiers.exit_margin_pct` (default 5) points below its line, a blind
  axis holds its last tier, and a page goes out against the highest tier paged
  since the last recovery, for one re-alert window. Sustained reminders, rises
  and recoveries still page; replaying that day's readings gives 8 pages.
