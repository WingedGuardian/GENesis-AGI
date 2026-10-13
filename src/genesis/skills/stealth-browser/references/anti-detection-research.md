# Anti-Detection Research Summary

Compiled 2026-04-22 from web research and bot-detection literature; revised
2026-10-04. Every number carries its source, or is marked **unsourced**
(community lore, kept as a hint, never as a fact to tune against).

## Fingerprinting (the browser engine's job)

Anti-detection browsers (Camoufox for Firefox, patchright for Chromium) cover
navigator/screen/WebGL/canvas/audio values, font lists, automation flags,
WebRTC leaks and statistically consistent fingerprints (BrowserForge-style
generation).

Known gaps:
- **Camoufox maintenance:** upstream paused roughly January-April 2026 and the
  engine fell behind (upstream called the Firefox 135 build out of date on
  2026-07-16, in its v152.0.4-beta.27 release note). Development resumed in
  `daijro/camoufox`; engine v156.0.1-beta.34 was released 2026-10-03 (stack
  survey, 2026-10-04). Genesis's `camoufox>=0.4` pin is an open lower bound
  with no lock file, so a fresh install gets the newest release (0.5.7 on
  PyPI as of 2026-10-05). The install surveyed on 2026-10-04 had camoufox
  0.4.11 with engine 135.0.1-beta.24. The Camoufox-specific behaviour
  statements in these skills (`browser_run_js` running in an isolated world,
  the "Camoufox 135" hidden-field result) were measured on that install and
  may not hold on a newer release.
- Camoufox cannot pass as Chrome: SpiderMonkey and V8 differ observably.
- Canvas spoofing quality degraded in some releases (**unsourced**).

## Behavioural signals

### Timing
- Human inter-key interval: mean 239 ms, SD 112 ms (Dhakal et al., "Observations
  on Typing from 136 Million Keystrokes", CHI 2018, Aalto University).
- Common bigrams faster, uncommon slower; slight fatigue over long text
  (**unsourced** magnitudes: 40% / 30% / 0.05% per char).
- Page load to first interaction 1.5-6 s, pre-submit pause 1.5-4 s, field to
  field 2-8 s (**unsourced**). The tools' delays (median ~3.3 s, see the stealth-browser skill)
  sit inside these ranges.

### Mouse
- Real movements produce many `mousemove` events with acceleration; scripted
  ones produce few or none, along straight lines (**unsourced** counts).
  Camoufox's `humanize` setting generates a cursor trail for the tools.

### Focus and input events
- Playwright's atomic `fill()` focuses the element and fires one `input` event
  (Playwright docs, `locator.fill`). It does not fire per-character
  `keydown`/`keypress`/`keyup`, which is what keystroke-dynamics checks look
  for. `browser_fill` types per keystroke on Camoufox and remote CDP for this
  reason.
- Events dispatched from page script carry `isTrusted=false`.

### Scroll
- Human scroll deltas vary (20-100 px) and include pauses and back-scrolls;
  scripted scrolls are uniform (**unsourced** numbers). Genesis has no
  humanized scroll tool. `browser_click` scrolls its own target into view,
  with Playwright's own scroll, not a humanized one.

### Paste
- Some systems distinguish typed from pasted input (**unsourced**). Genesis
  types; there is no paste tool.

### Honeypot fields
- Hidden by CSS (`display:none`, `visibility:hidden`, `opacity:0`), zero size or
  positioned off-screen; names like `url`, `website`, `fax`, `phone2`.
  Filling one flags the submission. Check computed style first (the stealth-browser skill has the
  `browser_run_js` snippet).

## Detection by platform

| Platform | Detection | Primary signals | Source |
|---|---|---|---|
| Cloudflare Turnstile | browser environment and JS challenges | fingerprint, challenge results | **unsourced** that typing/mouse are not scored |
| DataDome | behaviour + fingerprint | mouse, timing, request patterns | **unsourced** |
| reCAPTCHA v3 | risk score from the session | environment, engagement, history | Google publishes no feature weights |
| Ashby (ATS) | Google reCAPTCHA v3 at submit, plus post-submit fraud signals | fingerprint, IP | live test 2026-04-23 (`per-site/ats-ashby.md`) |
| Greenhouse (ATS) | reCAPTCHA v2 or v3, employer-configured | challenge | **unsourced**, 2026-04 |
| Lever (ATS) | rate limiting | request frequency | **unsourced**, 2026-04 |
| Reddit | in-house scoring, verification walls | account behaviour, IP, bursts | `per-site/reddit.md` |

## IP reputation (**unsourced**, community consensus as of 2026-04)
- Datacenter IPs are flagged hardest; residential IPs score better.
- GeoIP should agree with the browser's timezone and locale.
- Rotating IPs inside one session looks suspicious.

## Vendor notes (gathered June 2026, all **unsourced**)
- **AudioContext:** SwiftShader-identified audio output is reported as a
  PerimeterX and DataDome block trigger. Camoufox spoofs AudioContext; plain
  Playwright and custom CDP setups do not.
- **PerimeterX `_px3`:** reported to expire in about 60 s; the `_pxvid` visitor
  cookie should persist across pages in a session.
- **WebRTC:** behind a proxy, a WebRTC-exposed local IP that contradicts the
  proxy is a detectable inconsistency. Disable or proxy WebRTC.
- **reCAPTCHA v3 history:** an active Google login and prior solves are reported
  to raise the score. Google publishes no weights, so no number is given.
- **DataDome:** trains per-site models, so acceptable cadence differs by site.

## Calibration datasets

| Dataset | Content | Source |
|---|---|---|
| CMU Keystroke Dynamics | 51 subjects, 20,400 repetitions; hold/flight times (basis of the tools' key-hold distribution) | Killourhy and Maxion 2009, cs.cmu.edu/~keystroke |
| BlackTip | pre-fitted ranges from the CMU data | github.com/rester159/blacktip |
| Balabit Mouse Dynamics | 10 users, mouse trajectories | github.com/balabit/Mouse-Dynamics-Challenge |
| BeCAPTCHA-Mouse | ~9K trajectories incl. GAN-generated human paths | BiDA Lab (on request) |
