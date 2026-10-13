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
    "--executablePath", "<path to a Chrome or Chromium binary>",
    "--userDataDir", "${HOME}/.genesis/devtools-profile",
    "--chrome-arg=--no-sandbox"
  ]
}
```

- `--executablePath`: Genesis installs do not ship Google Chrome. Use any
  installed Chrome/Chromium. Playwright's own Chromium, if installed, is under
  `~/.cache/ms-playwright/chromium-*/`, in `chrome-linux64/chrome` on recent
  Playwright and `chrome-linux/chrome` on older releases; list the executable
  itself with `ls ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome`.
- `--userDataDir`: keep it separate from `~/.genesis/browser-profile`, which the
  Chromium fallback layer uses. Two Chromium processes on one profile fail to
  start or corrupt it.
- `--chrome-arg=--no-sandbox`: Chrome flags must go through `--chrome-arg`. A
  bare `--no-sandbox` is read as an option of chrome-devtools-mcp itself, which
  has no such option, and is dropped without a warning.

Then restart the CC session to pick up the new MCP server.

## Remote Browser (the user's Chrome)

Launching the user's Chrome for remote debugging and reaching its port is
described in ONE place: the `browser-automation` skill, "Remote CDP setup"
(`src/genesis/skills/browser-automation/SKILL.md`). Chrome 136+ ignores
`--remote-debugging-port` on the default profile, and headed Chrome binds the
port to loopback, so follow that section. Once the endpoint answers, replace
the config above with the following, using the same endpoint as
`GENESIS_CDP_URL` in place of `http://127.0.0.1:9222` (that value is the SSH
tunnel case):

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
`.mcp.json` and restart the session. This reclaims the ~17,000 chars of tool
descriptions that the 29 Chrome DevTools MCP tools add to the context.

## Token Cost

| Mode | Tools | Context Cost |
|------|-------|-------------|
| Genesis browser tools (always on) | 11 | ~4,600 chars of tool descriptions (measured 2026-10-04) |
| + Chrome DevTools MCP | +29 | ~17,000 chars |
| + Playwright MCP | +27 | ~13,700 chars |

Only activate external MCP servers when you need their specific capabilities.
