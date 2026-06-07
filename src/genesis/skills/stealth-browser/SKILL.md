---
name: stealth-browser
description: Anti-detection behavioral rules for stealth browser automation
consumer: cc_any
phase: execution
keywords: [browser, stealth, camoufox, cloudflare, turnstile, captcha, anti-bot, navigate, automation, vnc, click, medium, publish]
---

# Stealth Browser Skill

Tool-agnostic anti-detection behavioral rules for browser automation.
Applies regardless of browser engine (Camoufox, Playwright, future tools).

**Load this skill when:** The `browser_navigate` tool is used with the
default Camoufox (anti-detection) mode. The tool's docstring directs you here.

## Core Principle

Bot detection systems look for **behavioral signals**, not just
fingerprints. A perfect fingerprint with robotic behavior is still
detectable. The browser engine handles fingerprinting. This skill
handles behavior.

---

## Always-Headed Mode

Camoufox always launches headed on VNC display :99. This means:
- Human can always observe the browser via noVNC
- CAPTCHA/Turnstile escalation doesn't require browser restart
- `browser_collaborate(True)` speeds up timing (human watching)
- `browser_collaborate(False)` restores stealth timing (nobody watching)

---

## Layer 1: Current Infrastructure (use now)

These rules work with the existing browser tools. The tools already
implement human-like delays automatically — this section covers what
YOU (the LLM session) must do on top.

### Profile Pre-Warming

Before navigating to a target form (especially ATS job applications),
browse the company's public careers page first. This builds browsing
history and cookie trail in the persistent profile. A cold browser going
straight to an application form is a bot signal.

### Page Load Warm-Up

After `browser_navigate`, wait before interacting. Read the page snapshot,
plan your actions. Do NOT immediately call `browser_fill` or `browser_click`.
A human would look at the page first. 1-3 seconds minimum.

### Honeypot Detection

Before filling ANY form field, check the accessibility snapshot for hidden
fields. Do NOT fill fields that appear to be:
- `display: none` or `visibility: hidden` in the snapshot
- Zero-dimension elements
- Fields with names like `url`, `website`, `fax` that aren't expected
  for the form type (common honeypot names)
- Fields positioned off-screen

Filling a honeypot = instant bot detection. When in doubt, skip the field.

### Form Filling Order

Fill fields in visual top-to-bottom order, matching how a human would
tab through the form. Do NOT fill them in an arbitrary order.

### Pre-Submit Validation

Before clicking submit:
1. Take a `browser_screenshot()`
2. Review the screenshot to verify all fields are filled correctly
3. Only then click submit
4. Take another screenshot after submit to capture confirmation

### Error Recovery

If a form fill or click fails:
- Do NOT immediately retry — wait 2-5 seconds
- Take a screenshot to understand the current state
- Try an alternative selector
- If the page has changed (redirect, modal), re-read the snapshot

### Per-Site Escalation

Before engaging high-detection sites, check `references/per-site/` for
site-specific rules. High-detection sites include:
- **ATS systems**: Ashby, Greenhouse, Lever
- **Social platforms**: Reddit, X/Twitter, LinkedIn
- **Search engines**: Google
- **Tech communities**: Hacker News, Stack Overflow

If no per-site reference exists, conduct a research pass first to
understand the site's detection patterns before automating.

---

## Layer 2: Active Infrastructure

These features are now implemented in the browser tools.

### Per-Keystroke Typing (active)

`browser_fill` now types character-by-character with randomized inter-key
intervals (50-200ms) when Camoufox is active. This fires the full
keydown→keypress/input→keyup event chain per character. Atomic `fill()`
only fires a single `input` event — trivially detectable.

**Note for phone fields with input masks:** The per-keystroke typing
interacts with auto-formatting. If a phone field adds characters mid-type
(e.g., "(123) 456-..."), let the field format naturally — the typing
engine handles this. If the result looks wrong, retry with `browser_fill`
after clearing the field manually.

### Stealth Click (active)

`browser_click` now uses hover→mousemove trail→position jitter→realistic
mousedown/mouseup gap when Camoufox is active. Clicks land within the
central 60% of elements, not dead center. The Camoufox `humanize=2.5`
setting provides native Bézier cursor movement at the browser level.

### Turnstile/CAPTCHA Auto-Resolution (active)

After `browser_navigate`, Turnstile is automatically detected and
resolved. The resolution cascade runs without any manual intervention:

1. **Auto-resolve** (15s) — trusted browsers with cf_clearance clear instantly
2. **Widget click** — finds challenge container via DOM selectors and clicks
   with Camoufox's native Juggler input. This is the primary solver.
3. **playwright-captcha** — Shadow DOM traversal fallback
4. **VNC click** — real X11 input events via vncdotool (last resort)

**You do NOT need to:**
- Manually find or click Turnstile elements
- Use VNC/vncdotool yourself
- Write any challenge-bypass code
- Escalate to the user

Simply call `browser_navigate(url)` and check the response. If
`turnstile.status == "resolved"`, the page is ready. If `"blocked"`,
a Telegram alert was already sent.

---

## Layer 3: VNC Trusted Input Bridge (automatic fallback)

VNC click is the LAST fallback in the automated cascade (Phase 2 in
`_wait_for_turnstile`). You should almost never need to invoke it
manually — `browser_navigate` handles it automatically after the widget
click and playwright-captcha both fail.

This section documents the mechanism for debugging only. The VNC
protocol injects real input events, bypassing detection of synthetic
events (XTest, CDP, Playwright mouse).

### Why It Works

x11vnc injects input through the VNC protocol, adding network-realistic
timing and event sequencing patterns. While the underlying X11 mechanism
is similar to xdotool, the VNC protocol layer produces timing closer to
real human input (variable latency, natural event gaps). Playwright uses
CDP protocol which Cloudflare directly fingerprints. The practical result:
VNC-injected clicks pass Turnstile where xdotool and Playwright fail.

### When to Use

- Turnstile checkbox doesn't auto-resolve after 15 seconds
- reCAPTCHA v2 checkbox needs a human-like click
- Any anti-bot system that rejects automated clicks

### Prerequisites

- `genesis-vnc.service` running (x11vnc on port 5900, systemd auto-start)
- `vncdotool` installed (`pip install vncdotool`)
- If the VNC service uses password auth, start a temporary no-auth
  instance for programmatic use:
  `x11vnc -display :99 -forever -nopw -quiet -bg -rfbport 5999`

### Steps

1. **Find the element** — use `browser_run_js` to get the target
   element's bounding rect:
   ```javascript
   document.querySelector('[style*="display: grid"]').getBoundingClientRect()
   ```

2. **Get window geometry** — the browser window position on the Xvfb display:
   ```bash
   DISPLAY=:99 xdotool getwindowgeometry $(DISPLAY=:99 xdotool search --name "Camoufox")
   ```

3. **Calculate screen coordinates**:
   - `screen_x = window_x + page_element_x`
   - `screen_y = window_y + browser_chrome_height + page_element_y`
   - Chrome height is ~34px for Camoufox (viewport = window - 34)

4. **Send human-like mouse movement** — move through 1-2 intermediate
   points with 200-300ms pauses, then click:
   ```bash
   vncdo -s localhost::5999 move {start_x} {start_y}
   # pause 300ms
   vncdo -s localhost::5999 move {mid_x} {mid_y}
   # pause 200ms
   vncdo -s localhost::5999 move {target_x} {target_y} click 1
   ```

5. **Wait and verify** — allow 5-8 seconds for server-side verification,
   then check if the page title changed from "Just a moment..." to the
   actual page title.

### Notes

- The `cf_clearance` cookie persists for days/weeks after clearing
  Turnstile. Subsequent page loads won't trigger the challenge.
- If verification fails (checkbox reappears), wait 10 seconds and retry
  once. Cloudflare rate-limits rapid attempts.
- This technique works for ANY browser on Xvfb, not just Camoufox.

---

## Timing Profiles

The browser tools implement automatic delays. These are the profiles:

| Context | Inter-action delay | Notes |
|---------|-------------------|-------|
| Background (Camoufox, default) | 1-15s log-normal | Stealth priority. Mostly 2-5s, occasional long pauses |
| Collaborate (Camoufox + VNC) | 0.5-2s uniform | Human watching. Responsive but not instant |
| Playwright/Chromium (dev/test) | None | Speed priority. No stealth needed |

The delay fires automatically before `browser_fill`, `browser_click`,
and `browser_upload`. You do NOT need to add manual sleeps.

---

## Anti-Detection Services

See `references/services.md` for external services that supplement
the browser's built-in anti-detection:
- **2Captcha**: Programmatic CAPTCHA solving ($0.00145/solve)
- **Browserbase**: Cloud browser with real hardware ($0.002/session)
- **Residential proxies**: IP reputation improvement

---

## Advanced Behavioral Rules

These rules address detection surfaces beyond basic timing and clicks.
Apply them in all Camoufox sessions unless site-specific guidance overrides.

### Idle Mouse Micro-Jitter

Real hands produce ±1-3px tremor while "still." Between actions (any
dwell period >2s), the cursor must not be dead-still.

- Emit 1-3 mousemove events every 1-2s during all dwell periods
- Displacement: ±1-3px from current position (random, not oscillating)
- Do NOT jitter during active movement (only during stillness)
- Implemented in `_idle_jitter()` in browser.py

### Keystroke Hold Time (keydown-to-keyup gap)

Real keys are held briefly before release. Each keypress fires
`keyboard.down(char)` → hold → `keyboard.up(char)`.

- Hold time per key: sample from log-normal distribution
- Calibration: median ~86ms (p5=48ms, p95=149ms) per CMU Keystroke dataset
- Vary per keystroke -- NOT uniform across all keys
- Flight time (key-up to next key-down): existing 50-200ms IKI still applies
- Implemented in `_human_type()` in browser.py

### Navigation Graph Depth

Arriving directly at a form/auth page with no prior navigation is a
strong bot signal regardless of per-page behavior.

- For ANY target that is a form, login, checkout, or data-heavy endpoint:
  navigate through at least 2 prior pages on the same domain
- Dwell 2-5s on each prior page before proceeding
- This generalizes the existing "visit careers page" rule to all targets
- Referrer chain must be organic (not cold-start direct navigation)

### Tab Visibility Switch

Sessions maintaining continuous focus for >15s are atypical for humans.

- For pages with >15s dwell before form interaction: simulate one
  visibilitychange event (tab hidden + visible) before submission
- Use `browser_run_js` to dispatch: `document.dispatchEvent(new Event('visibilitychange'))`
- Or use actual tab switching if available

### Scroll Patterns

Real scrolling decelerates, occasionally goes backwards, and pauses at
content boundaries.

- Include at least one upward scroll segment per page (probability 0.2)
- Decelerate scroll to zero over 200-400ms at end of each gesture
- Add "scroll past then back" pattern when targeting form fields (p=0.3)
- Scroll delta variance: 20-100px per event (never constant)
- Implemented in `_human_scroll()` in browser.py

### Pre-Form Element Interaction

Direct-to-form behavior (first interaction is a form field) scores
-0.1 to -0.3 on reCAPTCHA v3.

- Before filling the first form field: move cursor to 1-2 non-form
  elements (nav link, header, image), dwell 0.3-1.5s each
- Then move to the first form field
- This produces an interaction graph that doesn't start at the form target

### Typo and Self-Correction (long text fields only)

Real typists make errors at ~2% rate and correct them.

- For text fields >20 characters: introduce one typo per ~50 chars
- Type 1-2 wrong characters, pause 200-500ms, backspace, type correct
- Do NOT apply to short fields (names, emails, passwords)
- This is LLM-guided behavior, not auto-enforced in code

---

## What This Skill Does NOT Cover

- Browser fingerprinting (handled by Camoufox at C++ level)
- Proxy/IP management (see references/services.md)
- Account management (login sessions, credential storage)
