---
name: browser-automation
description: Web and desktop automation with layered escalation (Fetch, Genesis Browser, Remote CDP, On-Demand MCP, Desktop), anti-detection, and persistent profiles
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

Genesis has five rungs of interaction — 1, 2, 3, 3b and 4. Choose the lightest
one that can accomplish the task. **Token cost increases as you climb.** Layer 4
leaves the browser entirely and is the only rung that reaches a non-web window.

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

### Layer 4: Desktop — the operator's whole machine, not just a browser
Reaches what no browser layer can: native applications, OS dialogs, and any
window that is not a web page.

**Nothing here is live today, on either leg.** Genesis holds no desktop
capture code and no input-injection code — verified against the tree, not
assumed. What exists is a deliberate build order for ACTUATION: the gate
(`src/genesis/autonomy/desktop_gate.py`, `config/desktop_takeover.yaml`) ships
first and alone, defaulting to refuse, needing two keys plus an out-of-band
grant to arm; the device-side actuator, the loop, the transport and the MCP
tool follow behind it. An actuator with a caller and no gate IS the ungated
capability, which is why the order is that way round.

So there are two routes to a desktop, and they are at different stages:

- **Ask a resident agent.** If the operator's machine already runs something in
  its interactive session that exposes capture or control, that is reachable
  NOW, needs no new Genesis code, and is the cheaper answer for perception.
- **Genesis's own actuator**, behind the gate above. In flight, inert, and not
  callable yet by design.

**What you must not do is build a third one.** No ad-hoc injector, no
hand-rolled capture path, nothing that reaches the desktop outside the gate.
That is the rule the deleted capture script carried and it still holds: the one
real advantage of wiring a desktop route as a tool is that Genesis could then
fire it autonomously, and that is the single property this capability should
not have yet.

**The mechanism, because it is not obvious and it decides the design.** An SSH
login on Windows lands in session 0 while the desktop lives in session 1 or
higher, so nothing run over SSH can see or touch the desktop — and it fails
*silently*, returning an empty well-formed result rather than an error. But the
boundary isolates window stations, **not sockets**: a session-0 process can
reach a loopback service in the desktop session perfectly well. That is the
whole reason asking beats driving. Full detail, including the empty-success
failure table: `docs/reference/windows-remote-execution.md`.

**Keep it un-callable.** The one real advantage of wiring a desktop route as a
tool is that Genesis could then fire it autonomously, and that is the single
property this capability should not have yet. A route reachable only from a
foreground session, through an operator-authenticated login, is the posture to
preserve until the gate covers the leg you want.

Cost is a screenshot per observation, so prefer a lower layer whenever the
target is reachable by one. A browser task is almost always cheaper at Layer 2
or 3.

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
| Native app / OS dialog / non-web window | Desktop | No browser layer can reach it |
| See what is actually on the operator's screen | Desktop | Ask the resident agent; a capture, not a page |

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

Applies to every layer that computes a position rather than naming an element.

Provenance, since it decides how much to trust each rule: the browser rules
below are checked against `mcp/health/browser.py` in this repo. The `SendInput`
and DPI-awareness material comes from a Windows spike whose code never landed
on main — it is retained because the semantics are Win32's, not ours, but
nothing here exercises it, so treat it as a specification to build against
rather than as described behaviour.

**Prefer a selector to a coordinate — but know which path you are on.**
Playwright's own click resolves the element, hit-tests the point it is about to
press, and *refuses* if something else is on top (`intercepts pointer events`).
That is the guarantee worth having. The catch is that stealth mode is the
DEFAULT in this codebase, and it does not take that path.

**Never mix coordinate spaces.** The recurring defect is arithmetic that adds
two numbers from different spaces:

| space | comes from |
|---|---|
| CSS pixels | `getBoundingClientRect()`, `outerHeight - innerHeight` |
| physical screen pixels | `xdotool` window geometry, a screen capture taken while DPI-aware |
| DPI-virtualised pixels | any Windows API read by a process that has not called `SetProcessDPIAware()` |
| normalised 0–65,535 | `SendInput` in absolute mode — see below |

**`SendInput` absolute mode is its own space, and it is not pixels.** With
`MOUSEEVENTF_ABSOLUTE`, `MOUSEINPUT.dx`/`.dy` are normalised to 0–65,535 — and
**which rectangle they normalise across is the part that bites.** Alone, the
flag maps that range onto the PRIMARY MONITOR only. Combined with
`MOUSEEVENTF_VIRTUALDESK` it maps onto the whole virtual desktop, whose origin
(`SM_XVIRTUALSCREEN`) can be NEGATIVE when a second display sits left of or
above the primary. A correct conversion therefore subtracts the virtual origin
before scaling by `SM_CXVIRTUALSCREEN` / `SM_CYVIRTUALSCREEN`:
`dx = (x - SM_XVIRTUALSCREEN) * 65535 / (SM_CXVIRTUALSCREEN - 1)`.

Get it wrong and the pointer lands correctly on the primary display and
silently wrong on every other one — the classic single-monitor-dev-machine
bug. And passing a pixel value through unconverted scales it by roughly
`65535 / width`: on a 1920-wide display a target at x=1000 lands about 29px
from the left edge, which reads as a near-miss rather than a unit error.

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

⚠ **Which paths actually do that, in `mcp/health/browser.py`:**

| click path | hit-tests the target? | reads back where it landed? |
|---|---|---|
| `page.click(selector)` — non-stealth `browser_click`, and every fallback | **yes** — Playwright refuses with `intercepts pointer events` | n/a, no coordinate |
| stealth `browser_click` (Camoufox, the DEFAULT) | no — `bounding_box()` then `mouse.move`/`down`/`up` | no |
| Turnstile widget click (`page.mouse.click(x, y)`) | no — raw coordinate input | no |
| shadow-DOM fallback (`el.click()`) | no — dispatches a DOM click, no coordinate | n/a |
| VNC input bridge (turnstile only) | no — `vncdo` has no concept of an element | **yes** — pointer position, drift vs intent, warns past 3px, then clicks anyway |

Read that as: **the guarantee exists, and the default path is not the one that
has it.** Stealth clicking trades the hit-test for a human-looking mouse
trail. It is not unchecked — an ambiguity guard runs on `text=` selectors and
the element must be visible first — but between reading the box and pressing
the button, nothing re-checks what is now under the point.

The VNC row is the subtle one: it verifies DELIVERY, not IDENTITY. It will tell
you the pointer arrived where you aimed. It cannot tell you the right thing was
there, and it clicks regardless, deliberately — refusing on drift would break
more than it saves.

So: treat any coordinate-driven click as UNVERIFIED, and confirm the outcome
afterwards by screenshot or a subsequent read, rather than trusting that a
failed aim would have been caught.

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
