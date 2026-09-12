---
name: browser-automation
description: Web automation with 4-layer escalation (Fetch, Genesis Browser, On-Demand MCP, Computer Use), anti-detection, and persistent profiles
consumer: cc_background_task
phase: 7
skill_type: workflow
keywords: [browser, navigate, click, fill, form, submit, login, scrape, automate, web, page, site, url]
---

# Browser Automation

## Purpose

Reference playbook for web automation tasks. Provides layer selection,
error recovery strategies, CSS selector patterns, form filling methodology,
and safety gates. Loaded when Genesis performs browser-based tasks.

## Browser Layers

Genesis has four layers of browser interaction. Choose the lightest layer
that can accomplish the task. **Token cost increases with each layer.**

### Layer 1: Web Fetch (read-only, public data — zero browser tokens)
- Tools: `WebFetch`, Firecrawl, `genesis.web` (SearXNG + Brave)
- Use for: public information retrieval, search, reading articles
- No authentication, no interaction, no JavaScript rendering
- Fastest and cheapest option

### Layer 2: Genesis Browser Tools (persistent profile, on-demand)
- Tools: `browser_navigate`, `browser_click`, `browser_fill`,
  `browser_upload`, `browser_press_key`, `browser_screenshot`,
  `browser_snapshot`, `browser_run_js`, `browser_sessions`,
  `browser_clear_domain`
- Available via genesis-health MCP (always loaded, ~800 chars token cost)
- Browser launches lazily on first navigation — zero overhead until used
- All blocking tools have a hard 60s timeout (configurable per tool) to
  prevent indefinite hangs. `browser_fill` scales with string length.
- **Default mode**: Camoufox (anti-detection Firefox). Always runs headed on
  VNC display :99 — observable via noVNC. Profile at `~/.genesis/camoufox-profile/`.
  Includes humanized cursor movement, per-keystroke typing with IKI jitter,
  and stealth click with hover/jitter.
- **Chromium fallback**: `browser_navigate(url, stealth=False)` uses Chromium
  for sites incompatible with Camoufox (rare). Profile at `~/.genesis/browser-profile/`.
- **Agent-owned accounts**: log into accounts created FOR the agent, never the
  user's personal accounts. Treat the agent like a new employee.
- **Collaborate mode**: `browser_collaborate(enable=True)` switches to fast
  timing (0.5-2s delays vs 1-15s stealth). The browser is ALWAYS visible on
  VNC — no mode switch or restart needed. Use when a human is actively
  watching to speed up interaction. Disabling restores stealth timing.

### Layer 3: Remote CDP (user's real Chrome over Tailscale)
- Tool: `browser_navigate(url, remote=True)` — built into genesis-health MCP
- Connects to user's Chrome via `playwright.chromium.connect_over_cdp()`
- **Real browser, real fingerprint** — nothing to detect. Bulletproof for
  ATS submissions with reCAPTCHA v3 or aggressive anti-bot detection.
- Collaborate timing auto-enabled (user watching their own screen)
- Drift detection: warns if user navigated away since last Genesis action
- **User setup**: Launch Chrome with `--remote-debugging-port=9222
  --user-data-dir=%USERPROFILE%\chrome-genesis` (Windows batch file)
- Set `GENESIS_CDP_URL=http://<tailscale-ip>:9222` in `secrets.env`, or
  pass `cdp_url=` parameter directly
- Graceful errors when Chrome is offline — no retry storms
- Camoufox stays default for non-adversarial browsing

### Layer 3b: On-Demand MCP (network inspection — activate when needed)
- Tools: Chrome DevTools MCP (29 tools) or Playwright MCP
- Activate: `/activate-browser` → adds to `.mcp.json` → restart session
- Deactivate when done to reclaim ~17k tokens of context budget
- Use when you need network inspection, Lighthouse, or performance tracing
  beyond what the built-in Genesis browser tools provide

### Layer 4: Computer Use — the operator's whole desktop, not just a browser
- Reaches what no browser layer can: native applications, OS dialogs, and any
  window that is not a web page. Also drives a browser when the point is to
  drive it *as the operator sees it*.
- **Perceive leg is live.** `scripts/capture_desktop.sh` triggers a capture on
  the operator's machine and returns the image. Window-scoped by default;
  full-screen exists but is never the default and is never inherited from a
  window grant (it discloses every open window at once).
- **Act leg — mouse, keyboard, drag — is NOT live**, and is gated behind an
  approval design. Do not attempt to construct it ad hoc.
- Mechanism, and it is not obvious: an SSH login on Windows lands in
  SessionId 0 while the desktop is SessionId 1, so **nothing run over SSH can
  see or touch the desktop**. Work reaches the desktop only via a scheduled
  task registered with an Interactive logon type.
  Full detail: `docs/reference/windows-remote-execution.md`.
- Cost is a screenshot per observation, so prefer a lower layer whenever the
  target is reachable by one. A browser task is almost always cheaper at
  Layer 2 or 3.

### Layer Selection Guide
| Need | Layer | Why |
|------|-------|-----|
| Read a public page | Fetch | No login needed |
| Search the web | Fetch | API-based, fast |
| Fill a form on agent's account | Genesis Browser | Persistent login |
| Site blocks automation | Genesis Browser (stealth) | Anti-detection |
| CAPTCHA / payment / 2FA step | Genesis Browser (collaborate) | User takes VNC control |
| ATS with reCAPTCHA v3 / Ashby | Remote CDP | Real Chrome fingerprint |
| Submit on user's logged-in site | Remote CDP | User's sessions |
| Network inspection / Lighthouse | On-Demand MCP | Chrome DevTools |
| Take action in user's banking app | Remote CDP | MUST confirm |
| Native app / OS dialog / non-web window | Computer Use | No browser layer can reach it |
| See what is actually on the operator's screen | Computer Use | Capture, not a page |

## When to Use

- Any task requiring interaction with a web page beyond simple fetching.
- Form filling, multi-step workflows, authenticated sessions.
- Data extraction requiring JavaScript rendering.
- Browser-based testing or verification.

## Selector Strategy

Try selectors in this priority order:

| Priority | Selector Type | Example | When |
|----------|--------------|---------|------|
| 1 | ID | `#submit-btn` | Element has unique ID |
| 2 | data-testid | `[data-testid="login"]` | Modern apps with test attributes |
| 3 | name attribute | `input[name="email"]` | Form fields |
| 4 | type attribute | `input[type="submit"]` | Standard form elements |
| 5 | Specific class | `.btn-primary` | Semantic class names |
| 6 | Visible text | text="Sign In" | Buttons and links |
| 7 | Composite | `form.login input[type="email"]` | When simple selectors aren't unique |

**Common patterns:**
```
# Forms
input[name="username"]
input[type="password"]
button[type="submit"]
select[name="country"]
textarea[name="message"]

# Navigation
nav a[href="/dashboard"]
header .menu-item
a:has-text("About")

# E-commerce
.product-card .price
button:has-text("Add to Cart")
.cart-total
```

## Error Recovery

| Error | Recovery Steps |
|-------|---------------|
| Element not found | 1. Try alternative selector 2. Try visible text 3. Scroll page 4. Wait for dynamic load |
| Page timeout | 1. Retry navigation 2. Check if URL redirected 3. Verify network connectivity |
| Login required | Inform user. Ask for credentials. Never guess passwords. |
| CAPTCHA | Switch to collaborate mode. User solves via VNC. Resume automation after. |
| Pop-up / modal | Click dismiss/close button. Look for `[aria-label="Close"]` or `.modal-close` |
| Cookie consent | Click "Accept" or dismiss. Look for `#cookie-accept` or text="Accept All" |
| Rate limited | Wait 30 seconds. Retry once. If still limited, back off exponentially. |
| Wrong page | Use page snapshot to verify. Navigate back. Check URL. |
| Stale element | Re-query the selector. Page may have re-rendered. |

## Form Filling Workflow

1. **Read page** — Take snapshot to understand form structure
2. **Identify fields** — Map each required field to a selector
3. **Fill sequentially** — One field at a time, verify each
4. **Handle dropdowns** — Use select_option for `<select>`, click+text for custom dropdowns
5. **Handle checkboxes** — Click to toggle, verify state after
6. **Screenshot before submit** — Visual verification before irreversible action
7. **Submit** — Click submit button
8. **Verify result** — Read resulting page to confirm success

## Coordinate Safety

Applies to every layer that computes a position rather than naming an element,
and to the desktop layer especially. Two of these were live bugs in this
codebase, found 2026-09-06.

**Prefer a selector to a coordinate.** `browser_click` resolves the element
through Playwright, so there is no coordinate to get wrong. A computed
coordinate can be wrong for reasons the computation cannot see. Reach for
coordinates only when nothing else can hit the target.

**Never mix coordinate spaces.** The recurring defect is arithmetic that adds
two numbers from different spaces:

| space | comes from |
|---|---|
| CSS pixels | `getBoundingClientRect()`, `outerHeight - innerHeight` |
| physical screen pixels | `xdotool` window geometry, `SendInput` absolute mode |
| DPI-virtualised pixels | any Windows API read by a process that has not called `SetProcessDPIAware()` |

They coincide **only at `devicePixelRatio == 1` and 100% display scaling**,
which is why this class of bug sits dormant on an unscaled dev machine and
breaks on a real laptop. On Windows at 125% scaling a DPI-unaware process is
told the screen is 1536x864 when it is 1920x1080 — every measurement it then
takes is wrong by 1.25, silently.

**Set DPI awareness before the first measurement, not before the first use.**
Anything measured beforehand is already in the wrong space.

**Confirm the target, then act.** After positioning and before clicking, read
back what is actually under the pointer and refuse if it is not the element
intended. This converts every coordinate error — scaling, stale bounds, a
window that moved between measuring and acting — from a wrong click into a
refusal.

**Log where it actually landed, not where you aimed.** Intent is a
computation; a computation cannot notice that it is wrong. Record actual
against intended with the drift and the scale factor, at the moment of the
action, so a mis-click leaves a forensic trail instead of a log that looks
correct. If the readback fails, say so — "unknown" must never render as fine.

**A zero return is a refusal.** `SendInput` returns the number of events
injected and returns `0` rather than raising when the OS declines. Any API of
this shape must have its return value checked; ignoring it reads as success.

## Safety Gates

**MANDATORY before any financial transaction:**
1. Summarize what will be purchased/paid
2. Show total cost
3. Get explicit user confirmation
4. Never auto-complete purchases
5. Never click "Place Order", "Pay Now", "Confirm Purchase" without approval

**MANDATORY for credential entry:**
1. Verify the domain is correct (check URL bar, not page content)
2. Warn on HTTP (non-HTTPS) credential pages
3. Never store passwords
4. Never enter credentials on unfamiliar domains without user confirmation

## Session Management

- Browser sessions persist within a task/conversation
- Cookies and login state are maintained across page navigations
- Close browser explicitly when done to free resources
- If session needs to survive across tasks, document the auth state needed

## Multi-Step Workflow Pattern

For complex workflows (e.g., fill form → verify → submit → navigate → extract):

1. **Plan the steps** — List all pages and actions before starting
2. **Checkpoint after each page** — Take snapshot, verify you're in the right place
3. **Handle branching** — If the workflow can branch (success/error), plan for both
4. **Limit scope** — Max 10-20 page navigations per task to prevent runaway browsing
5. **Report progress** — Log each completed step

## Output Format

```yaml
task_id: <BROWSER-YYYY-MM-DD-NNN>
pages_visited: <count>
actions_taken:
  - action: <navigate | click | type | select | screenshot>
    target: <selector or URL>
    result: <success | failed | recovered>
errors_recovered: <count>
screenshots: [<file paths>]
result: <task outcome description>
```

## References

- Genesis browser MCP tools: `browser_navigate`, `browser_click`, `browser_fill`,
  `browser_upload`, `browser_press_key`, `browser_screenshot`, `browser_snapshot`,
  `browser_run_js`, `browser_sessions`, `browser_clear_domain`,
  `browser_collaborate` (via genesis-health MCP)
- `src/genesis/mcp/health/browser.py` — MCP tool implementations
- `src/genesis/browser/profile.py` — BrowserProfileManager (cookie DB, sessions)
- `scripts/browser.py` — Standalone CLI (opens/closes per command, for one-off use)
- `src/genesis/skills/osint/SKILL.md` — For web-based investigation
- `/activate-browser` command — On-demand Chrome DevTools MCP activation
