- **The OpenClaw completions endpoint now requires a bearer token, and existing
  installs must add one.** `POST /v1/chat/completions` previously accepted
  unauthenticated requests; it now refuses them, matching every other `/v1/*`
  route. The dashboard's session gate exempts that whole prefix by design —
  machine callers have no browser session — so each such route carries its own
  bearer check, and this one now does too.

  **What to do when you update.** Set `GENESIS_MCP_HTTP_TOKEN` in `secrets.env`
  if it is not already set, and put that same value in your OpenClaw provider
  config as the API key — it is an ordinary OpenAI-compatible client, so the
  header needs no custom code. Until you do, the endpoint answers 401. With the
  variable unset it answers 503, and the boot-time warning now names every
  surface that depends on it rather than the voice API alone.

  The repo's own setup instructions for that client were stale in the same
  direction — a placeholder key and the wrong port — and have been corrected.

- **Two refusals on the `/v1` surface got less confusing.** A non-ASCII
  `Authorization` header raised inside the constant-time comparison and surfaced
  as a 500 instead of a 401; the comparison is now on bytes. And a quoted
  whitespace-only token in `secrets.env` counted as configured, which a blank
  credential then satisfied; it is stripped now, as the dashboard password
  already was. Both had been true of every `/v1/voice/*` route since they were
  written, and are fixed once rather than three times because the check finally
  has a single implementation.
