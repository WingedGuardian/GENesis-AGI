- **Daily free-tier budgets for providers.** Providers can now carry
  `rpd_limit` (requests/UTC-day) and `tpd_limit` (tokens/UTC-day) in
  `config/model_routing.yaml`, each in the provider's own unit. When a
  provider's daily budget is spent, routing deselects it until the next UTC
  day instead of burning the rest of the day on 429s that silently push load
  onto paid fallbacks — you get one warning event at the crossing, and the
  live counters appear in the dashboard's routing config API
  (`daily_budget`). Groq ships with its measured caps (200K tokens/day,
  1K requests/day); other providers are opt-in. Counters survive restarts
  (`~/.genesis/routing_budget_state.json`) and err toward undercounting, so
  a miscount can never lock a healthy provider out. Kill switch:
  `GENESIS_DAILY_BUDGET_DISABLED=1`.