- **Genesis can now be the brain behind a desktop assistant.** Any tool that
  speaks OpenAI chat-completions to a configurable `base_url` can be pointed at
  `POST /v1/desk/chat/completions` instead of a model vendor. Every turn then
  routes through Genesis's router, so provider choice, cost tracking, circuit
  breakers and observability are Genesis's — and the desktop holds no model
  credential of its own. Authenticate with `GENESIS_MCP_HTTP_TOKEN`, the same
  token the voice API uses.

  **Two lanes, because the turns are not the same shape.** `desk_primary` leads
  with a capable model: a client that fires its own tools by emitting an exact
  control tag needs instruction-following, since a near-miss silently does
  nothing. `desk_fast` leads with a fast one, for replies composed while a
  call is live. Select with the `X-Genesis-Lane` header, or with a `-fast` /
  `-primary` suffix on `model` where a proxy strips unknown headers; anything
  unrecognised resolves to the capable lane, because being quietly served from
  the fast one is the harder failure to notice. Both chains are configurable in
  `config/model_routing.yaml`.

  Honest about what it is not. It performs no memory recall — a client that
  builds its own system prompt should reach Genesis memory through an explicit
  tool, not by injection, or two context builders end up fighting over one
  prompt. It refuses streaming, tool-call turns, and image content: the routing
  layer is text-only, and answering an image question from the text alone would
  describe something never seen. Every refusal is an error object with no
  `choices` key, so a client parsing the completion raises rather than speaking
  the error aloud as if it were the answer.

  A provider that answers with no content at all is one of those refusals, not
  an empty reply passed through: the endpoint returns 502 rather than handing
  back a blank turn for the desktop to speak as silence. Note the limitation
  that sits behind it — the router stops its fallback walk on the first
  success, and a blank answer counts as one, so the later links in the chain
  are not tried. Fixing that belongs to the router and every caller it serves
  rather than to this endpoint, and is tracked separately.

- **Every `/v1` route is now checked for authentication by a test, not a
  convention.** The suite enumerates them from the live route map and asserts
  each refuses an anonymous caller, so a surface added later is covered without
  anyone remembering to add it. A route that should be public has to be named,
  with a reason, where a reviewer will see it.
