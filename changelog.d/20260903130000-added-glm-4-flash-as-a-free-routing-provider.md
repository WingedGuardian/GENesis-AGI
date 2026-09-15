- **GLM-4 Flash as a free routing provider (Zhipu direct).** A new
  `zhipu-glm-flash` provider reaches Zhipu's published free model on the
  general OpenAI-compatible endpoint using the standard `ZHIPU_API_KEY`. It
  sits immediately ahead of the paid DeepSeek fallbacks in 12 chains — never
  ahead of a deliberately-paid lead, and not promoted above the free
  providers that precede the paid fallback — so overflow that previously
  went straight to paid models now tries a free, vendor-independent lane
  first. Installs without the key are unaffected (the provider
  auto-disables, as always); installs that set `ZHIPU_API_KEY` for the CC
  roster's glm failover share that key here, so it must be valid on the
  general endpoint (a coding-plan-only key just 401s this lane —
  the chain continues past it).
