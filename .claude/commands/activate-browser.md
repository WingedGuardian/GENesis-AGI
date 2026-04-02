---
name: activate-browser
description: Activate Chrome DevTools MCP for heavy browser sessions (network inspection, Lighthouse, performance tracing). Deactivate when done.
---

# Activate Browser (On-Demand MCP)

Add Chrome DevTools MCP to `.mcp.json` for sessions that need full browser
capabilities beyond the built-in genesis-health browser tools.

## When to Use

- You need network request inspection or console access
- You need Lighthouse audits or performance tracing
- You need to connect to a remote Chrome instance (user's browser)
- The built-in `browser_navigate`/`browser_click` tools aren't sufficient

## Activation

Add this entry to `~/genesis/.mcp.json` under `mcpServers`:

```json
"chrome-devtools": {
  "command": "npx",
  "args": [
    "chrome-devtools-mcp@0.21.0",
    "--headless",
    "--executablePath", "/usr/bin/google-chrome",
    "--userDataDir", "${HOME}/.genesis/browser-profile",
    "--no-sandbox"
  ]
}
```

Then restart the CC session to pick up the new MCP server.

## Remote Browser (CDP-over-SSH)

To connect to the user's real Chrome:

1. User launches Chrome with `--remote-debugging-port=9222`
2. Set up SSH tunnel: `ssh -N -L 9222:localhost:9222 user@host`
3. Replace the config above with:

```json
"chrome-devtools-remote": {
  "command": "npx",
  "args": [
    "chrome-devtools-mcp@0.21.0",
    "--browserUrl", "http://127.0.0.1:9222"
  ]
}
```

## Deactivation

When done with heavy browser work, remove the `chrome-devtools` entry from
`.mcp.json` and restart the session. This reclaims ~17k tokens of context
budget that the 29 Chrome DevTools MCP tools consume.

## Token Cost

| Mode | Tools | Context Cost |
|------|-------|-------------|
| Genesis browser tools (always on) | 8 | ~800 chars |
| + Chrome DevTools MCP | +29 | ~17,000 chars |
| + Playwright MCP | +27 | ~13,700 chars |

Only activate external MCP servers when you need their specific capabilities.
