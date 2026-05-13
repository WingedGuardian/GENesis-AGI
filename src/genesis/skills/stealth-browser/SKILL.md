---
name: stealth-browser
description: Anti-detection behavioral rules for stealth browser automation
consumer: cc_any
phase: execution
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

### Turnstile/CAPTCHA Auto-Escalation (active)

After `browser_navigate`, Turnstile is automatically detected. If it
doesn't auto-resolve in 15 seconds, try the Layer 3 VNC trusted input
technique (below) before escalating to human. If VNC click also fails,
a Telegram alert is sent and the system polls for human resolution via
VNC for up to 5 minutes. No browser restart needed — always headed.

---

## Layer 3: VNC Trusted Input Bridge

When Turnstile or other anti-bot checkboxes won't auto-resolve, use the
VNC protocol to inject real input events. This bypasses detection of
synthetic events (XTest, CDP, Playwright mouse).

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

## What This Skill Does NOT Cover

- Browser fingerprinting (handled by Camoufox at C++ level)
- Proxy/IP management (see references/services.md)
- Account management (login sessions, credential storage)
