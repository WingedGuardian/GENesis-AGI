- Inbox evaluations now receive the complete canonical `evaluate` and
  `user_evaluate` skills in a deterministic system prompt. Inbox scans stop
  before changing durable state if any required instruction component is
  missing, unreadable, or empty, instead of silently using a reduced fallback.
