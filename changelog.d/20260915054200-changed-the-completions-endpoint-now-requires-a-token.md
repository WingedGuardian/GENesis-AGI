- **The OpenClaw completions endpoint now requires a bearer token, and existing
  installs must add one.** `POST /v1/chat/completions` invokes Claude Code, and
  it enforced no authentication: the dashboard's session gate exempts the whole
  `/v1/*` prefix because machine callers have no browser session, and each such
  route is expected to carry its own bearer check. This one never did, so on a
  host that binds beyond loopback anything able to reach the port could spawn CC
  subprocesses.

  **What to do when you update.** Set `GENESIS_MCP_HTTP_TOKEN` in `secrets.env`
  if it is not already set, and put that same value in your OpenClaw provider
  config as the API key — it is an ordinary OpenAI-compatible client, so the
  header needs no custom code. Until you do, the endpoint answers 401. With the
  variable unset it answers 503, and the boot-time warning now names both this
  endpoint and the voice API rather than the voice API alone.

  The repo's own setup instructions for that client were stale in the same
  direction — a placeholder key and the wrong port — and have been corrected.

- **Two refusals on the `/v1` surface got less confusing.** A non-ASCII
  `Authorization` header raised inside the constant-time comparison and surfaced
  as a 500 with a stack trace per request instead of a 401; the comparison is now
  on bytes. And a quoted whitespace-only token in `secrets.env` counted as
  configured, which a blank credential then satisfied; it is stripped now, as the
  dashboard password already was. Both had been true of every `/v1/voice/*` route
  since they were written, and are fixed once rather than three times because the
  check finally has a single implementation.
