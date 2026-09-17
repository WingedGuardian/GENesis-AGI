"""Genesis OpenClaw hosting adapter.

Exposes a /v1/chat/completions endpoint so OpenClaw can treat Genesis as a
custom LLM provider.  OpenClaw handles all channel I/O (WhatsApp, Telegram,
Slack, Discord, etc.); Genesis handles all intelligence.

Usage — add to ~/.openclaw/openclaw.json:

    {
      models: {
        mode: "merge",
        providers: {
          genesis: {
            baseUrl: "http://127.0.0.1:5000/v1",
            // The endpoint requires a bearer token: paste the value of
            // GENESIS_MCP_HTTP_TOKEN from secrets.env. Any other value is a 401.
            apiKey: "<GENESIS_MCP_HTTP_TOKEN>",  // pragma: allowlist secret
            api: "openai-completions",
            models: [{
              id: "genesis", name: "Genesis",
              reasoning: false, input: ["text"],
              cost: {input:0, output:0, cacheRead:0, cacheWrite:0},
              contextWindow: 200000, maxTokens: 8192,
            }],
          },
        },
      },
      agents: { defaults: { model: { primary: "genesis/genesis" } } },
    }
"""

from genesis.hosting.openclaw.adapter import OpenClawAdapter

__all__ = ["OpenClawAdapter"]
