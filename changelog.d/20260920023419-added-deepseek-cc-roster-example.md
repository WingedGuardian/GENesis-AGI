- **The CC model roster config now documents DeepSeek as a worked failover peer,
  including a billing trap it is easy to walk into.** The roster is what runs your
  Claude Code turn on another provider when the Anthropic subscription hits its
  usage cap, and the shipped config file explains how to declare a peer without
  shipping one. DeepSeek previously appeared only in a list of vendor-published
  endpoints nobody here had tried; it is now a full example with its endpoint,
  model id and credential variable.

  The reason it earned its own entry is cost. DeepSeek's Anthropic-compatible
  endpoint bills per token, and it silently maps model names it does not
  recognise rather than rejecting them: an unsupported name becomes its cheap
  Flash model, while anything beginning `claude-opus` becomes its Pro model and is
  billed at the Pro price. The vendor's own quickstart sets only the base URL and
  the key, which leaves Claude Code's opus-tier variable holding a Claude name —
  so opus-tier calls quietly bill at the higher rate. Configuring the peer through
  the roster avoids this, because the roster sets every model variable to the
  peer's own id; the note is there for anyone wiring it up by hand instead.
