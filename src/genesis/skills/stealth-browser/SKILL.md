---
name: stealth-browser
description: Anti-detection behaviour for Genesis browser automation - what the Camoufox tools already do, what the agent must still do, Cloudflare Turnstile handling, and per-site notes for bot-hostile sites
consumer: cc_any
phase: execution
keywords: [browser, stealth, camoufox, cloudflare, turnstile, captcha, recaptcha, anti-bot, bot, detection, honeypot, fingerprint, navigate, automation, vnc, click, medium, publish]
---

# Stealth Browser

Behaviour rules for sites that try to detect automation. Read the
`browser-automation` skill first: layers, timeouts, safety gates,
verify-after-act, overlays and closing browsers live there and are not repeated
here.

Bot detection scores the environment (fingerprint, IP, how the browser is
driven) and the behaviour. The browser engine and the tools cover most of
both. Your job is the part only a planner can do: what to visit, in what order,
what not to touch, and when to stop.

## What the tools already do

Do not re-implement any of this.

| Behaviour | Where | Detail |
|---|---|---|
| Fingerprint | Camoufox engine | Firefox-based, spoofed at engine level |
| Delay before `browser_click`, `browser_fill`, `browser_upload` | all layers below | see Timing |
| Per-keystroke typing | Camoufox, remote CDP | field cleared first; key hold log-normal, median 86 ms (clamped 30-200 ms, p95 ~153 ms); 50-200 ms between keys; 5% of gaps are 0.3-1 s pauses. Chromium and TinyFish fill atomically |
| Cursor | Camoufox | `humanize=2.5` cursor trail, hover, 50-200 ms dwell, click inside the central 60% of the element, 40-120 ms press. No scroll and no hit test: see `browser-automation`, "Clicking today" |
| Turnstile | Camoufox, Chromium | detected and worked on inside `browser_navigate` (below) |
| Keyboard repeat | `browser_press_key` | 50-150 ms between repeats, no pre-delay |

**Not done for you, and not to be faked:** idle cursor jitter, humanized
scrolling, tab visibility changes. `_idle_jitter()` and `_human_scroll()` exist
in `browser.py` with no call site. Do not emulate them with `browser_run_js`:
script-dispatched events carry `isTrusted=false` and a synthetic
`visibilitychange` leaves `document.visibilityState` at `visible`, so both are
easier to detect than doing nothing.

### Timing
| Context | Delay before click/fill/upload |
|---|---|
| Camoufox (default) | 1-15 s log-normal, median ~3.3 s, p90 ~7 s |
| Camoufox with `browser_collaborate(True)` | 0.5-2 s uniform |
| Remote CDP | 0.5-2 s uniform, always |
| Chromium, TinyFish | none |

Collaborate timing stays on after any `browser_navigate(..., remote=True)` call
and after a VNC hand-off. Call `browser_collaborate(False)` before stealth work
in Camoufox, or every later action runs at the fast 0.5-2 s pace.

The delays are automatic. Never add sleeps, and never "wait 1-3 s" yourself.
Spend the time reading the snapshot and planning the next action instead.

## What you must do

1. **Arrive like a visitor.** Before a target form, login or checkout, open the
   site's public page (careers page, home page) in the same profile and reach
   the target by clicking links. `browser_navigate` sends no referrer, so a
   cold navigate straight to a deep form URL has no history behind it. Benefit
   is plausible but unmeasured; skip it when the form URL is the only entry.
2. **Check for honeypots before filling.** The snapshot is an accessibility
   tree: it drops `display: none` fields and says nothing about opacity, clip,
   overflow, size or position, so it cannot tell you which field is a
   honeypot, while a selector can still reach one. Run this with
   `browser_run_js`; it returns only the suspect controls, each with reasons:
   ```js
   (() => {
     const why = el => {
       const r = [], b = el.getBoundingClientRect();
       for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
         const s = getComputedStyle(n);
         if (s.display === 'none') r.push('display:none');
         if (s.visibility === 'hidden') r.push('visibility:hidden');
         if (+s.opacity === 0) r.push('opacity:0');
         if (s.clip && s.clip !== 'auto') r.push('clip');
         if (s.clipPath && s.clipPath !== 'none' && (n === el || /inset\((50|100)%|circle\(0/.test(s.clipPath))) r.push('clip-path');
         if (n === el && n.getAttribute('aria-hidden') === 'true') r.push('aria-hidden');
         if (n !== el && n !== document.body && n !== document.documentElement) {
           const a = n.getBoundingClientRect(), cuts = v => v === 'hidden' || v === 'clip';
           const ox = Math.min(b.right, a.right) - Math.max(b.left, a.left);
           const oy = Math.min(b.bottom, a.bottom) - Math.max(b.top, a.top);
           if ((cuts(s.overflowX) && ox < 2) || (cuts(s.overflowY) && oy < 2)) r.push('clipped by ancestor');
         }
       }
       if (b.width < 2 || b.height < 2) r.push('zero size');
       if (b.left >= innerWidth || b.right <= 0 || b.bottom + scrollY <= 0) r.push('off-screen');
       return [...new Set(r)];
     };
     const out = [];
     for (const e of document.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio]),textarea,select')) {
       const r = why(e);
       if (r.length) out.push({field: e.name || e.id, type: e.type, reasons: r});
     }
     for (const e of document.querySelectorAll('input[type=checkbox],input[type=radio]')) {
       const label = e.labels && e.labels[0];
       const r = label ? why(label) : ['no label'];
       if (r.length) out.push({field: e.name || e.id, type: e.type, reasons: r.map(x => 'label: ' + x)});
     }
     return out;
   })()
   ```
   It checks text-like fields and selects against their own style and every
   ancestor's, including an ancestor with `overflow: hidden` that cuts the
   field off. Checkboxes and radios are judged by their `<label>`, because
   real sites routinely hide the native control (opacity 0,
   `appearance: none`) behind a styled label; file inputs are skipped for the
   same reason. Fields below the fold and inside scrollable boxes are not
   reported. Never fill a reported field. A checkbox or radio reported with
   `no label` may be a custom control: confirm with a screenshot. If every
   field is reported, a dialog is probably open: dismiss it and re-run.
   Measured 2026-10-04 on a fixture, identical in Chromium (headed) and
   Camoufox 135 (headless): 13 of 13 hidden fields reported, 0 of 10
   legitimate controls (styled checkbox, radio and file input, a rounded
   `clip-path` container, a scroll container, below-the-fold fields, a page
   with `overflow-x: hidden` on `html` and `body`, an `aria-hidden` ancestor).
3. **Fill in visual order**, top to bottom, the way a person tabs through.
4. **Use the tools for every interaction.** Never click, type or submit through
   `browser_run_js` (`el.click()`, `dispatchEvent`, setting `.value`) on a
   protected site: those events are untrusted. Reading with it is fine.
5. **Type exactly what is meant.** No deliberate typos: `browser_fill` clears
   the field and types the whole value, so a "correction" cannot be appended
   and a typo can land in the submitted text.
   Fields with input masks (phone, dates) reformat as you type; if the read-back
   value is wrong, call `browser_fill` again with the raw digits.
6. **On a failed action**, re-read the snapshot before retrying, retry once with
   a better selector, and decide data rejection versus bot detection
   (`browser-automation`, Diagnosis) before switching layers.
7. **Pace the site, not the page.** Space repeated submissions from one
   identity (several minutes apart for applications) and avoid bursts.
8. **Check `src/genesis/skills/stealth-browser/references/per-site/`** (in the
   Genesis repo) before Ashby, Greenhouse, Lever or Reddit. For another
   high-detection site (X, LinkedIn, Google, Hacker News, Stack
   Overflow) with no file, research its detection first.

## Cloudflare Turnstile

`browser_navigate` handles it (Camoufox and Chromium; not remote CDP or
TinyFish). Read `turnstile` in the result:
- absent: no challenge detected, OR detection itself errored (the cascade
  returns nothing when it throws). Check the result's `title` is not
  "Just a moment..." before treating the page as loaded.
- `resolved`: page is ready. `method` says how.
- `embedded`: the page only embeds a widget on already-loaded content; no
  interstitial. It matters only if a later submit needs the token.
- `blocked`: not resolved. The tool attempted a Telegram alert (skipped when
  Telegram credentials are missing). Hand off to the user
  (`browser-automation`, Safety gates) or stop. Do not loop navigations;
  Cloudflare rate-limits rapid retries, so wait at least 10 s before one retry.

The cascade for a blocking interstitial, in order: auto-resolve poll (15 s),
widget click (up to 3), `playwright-captcha` solver, VNC click (up to 3), page
reload plus 2 more VNC clicks, then `blocked`. `playwright-captcha` and
`vncdotool` are optional and not Genesis dependencies; when absent those phases
fail with nothing in the tool result (only the server log). The full cascade
can run past the 300 s navigate timeout: you then get a timeout error with no
`turnstile` field and a reset page. Check the title on the next navigate
before retrying.

`cf_clearance` (the cookie that skips the challenge next time) lasts 30 min by
default, configurable per site; Cloudflare recommends 15-45 min (Cloudflare
docs, "Challenge Passage", read 2026-10-04). Do not assume a cleared challenge
persists across a long task.

### Debugging the VNC click by hand
Only to debug the cascade; the tool does this itself. The VNC bridge injects
OS-level pointer events (x11vnc delivers them through XTEST); whether that
passes Turnstile more often than the widget click is unmeasured.
- Server: `genesis-vnc.service`, x11vnc on display `:99`, port 5999, password
  auth. Talk to it with `vncdo -s 127.0.0.1:99 -p "${GENESIS_VNC_PASSWORD:-genesis}"`
  (display notation; `localhost::5999` fails over IPv6). Never start another
  VNC server, and never one without a password: the display shows logged-in
  sessions, and when the service is down the tools kill a foreign x11vnc
  holding port 5999. The tools start x11vnc themselves only when the
  `systemctl` binary is missing or a `systemctl` call times out; they use the
  password only if `~/.genesis/vnc_passwd` exists, so check that it does
  before relying on VNC. If systemd is present but the unit is missing or
  fails to start, nothing starts and VNC stays down.
- `vncdotool` is optional (`vncdo` may be absent).
- Window origin: `DISPLAY=:99 xdotool getactivewindow getwindowgeometry`, as the
  code does. Display `:99` is shared, so confirm the active window is this
  session's Camoufox.
- Convert with `vnc_click_target()` in `browser.py`:
  `x = win_x + left * dpr`, `y = win_y + (chrome_h + top) * dpr`, where
  `left`/`top` come from `getBoundingClientRect()`, `chrome_h` from
  `outerHeight - innerHeight` and `dpr` from `devicePixelRatio`.
- Move and click as separate `vncdo` calls, then allow 5-8 s and check that the
  title left "Just a moment...".
- reCAPTCHA is not handled by this path; its targeting knows Cloudflare
  selectors only.

## When stealth is not enough

- **Fingerprint scoring** (reCAPTCHA v3, enterprise anti-bot): behaviour cannot
  fix a fingerprint score. Move to remote CDP (the user's Chrome) with the user,
  or hand the step to the user.
- **Paid services** (TinyFish, CAPTCHA solvers, cloud browsers, residential
  proxies): `src/genesis/skills/stealth-browser/references/services.md` (in the Genesis repo). Every paid use needs the user's approval,
  each time.
- **IP reputation**: a datacenter IP is the strongest single signal on several
  sites; there is no tool-level fix. Ask the user.

## References
Paths are relative to the Genesis repo root:
- `src/genesis/skills/stealth-browser/references/anti-detection-research.md`: detection signals and vendor notes.
- `src/genesis/skills/stealth-browser/references/services.md`: paid services, with dated prices.
- `src/genesis/skills/stealth-browser/references/per-site/`: `ats-ashby.md` (Ashby, Greenhouse, Lever), `reddit.md`.
