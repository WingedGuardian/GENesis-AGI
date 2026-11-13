- **Outbound scanning and credential redaction catch more credential shapes.**
  An email or Discord send, and an email subject, are now refused when they
  carry a JWT (including one in an `Authorization: Bearer` header), a Discord
  webhook URL, a Telegram bot token, or a GitLab, NVIDIA, Tavily, Firecrawl,
  Hugging Face, xAI, Slack, AWS, Google, DeepInfra, Cerebras, Stripe, npm or
  PyPI key. Peer replies and the failure text kept from remote runs redact
  the same shapes in place, and peer messages, approval details and tool
  summaries that carry one are refused or withheld.

  These shapes come from one shared table that favours catching a credential
  over sparing a lookalike, so a benign string with the same shape (a long
  slug after `glpat-`, a `<digits>:<32 hex characters>` pair) is refused too.
  A send that fails with `Content scan quarantine: ['credential_...']` names
  the shape that matched.
