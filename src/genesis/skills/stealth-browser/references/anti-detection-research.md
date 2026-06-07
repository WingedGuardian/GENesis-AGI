# Anti-Detection Research Summary

Compiled: 2026-04-22. Source: web research + bot detection literature.

## Fingerprinting (browser engine handles this)

Modern anti-detection browsers (Camoufox, Patchright) handle:
- Navigator/screen/WebGL/canvas/audio fingerprinting
- Font enumeration spoofing
- Playwright sandboxing removal
- Headless mode masking
- HTTP header normalization
- WebRTC IP leak prevention
- BrowserForge statistical fingerprint generation

**Known gaps (as of 2026-04):**
- Camoufox has a maintenance gap — base Firefox version slightly outdated
- Cannot masquerade as Chrome (SpiderMonkey vs V8 is unfakeable)
- Canvas spoofing quality has degraded in recent versions

## Behavioral Signals (our responsibility)

### Timing
- Human inter-keystroke interval: 239ms mean, 112ms SD (Aalto 136M study)
- Common bigrams 40% faster, uncommon 30% slower
- Gradual fatigue: ~0.05% slower per character
- Page load to first interaction: 1.5-6 seconds (immediate = bot)
- Pre-submit think time: 1.5-4 seconds
- Form field to field: 2-8 seconds (log-normal, not uniform)

### Mouse Movement
- Humans generate hundreds of mousemove events per movement
- Bots generate 0-10 events (or none)
- 65% of fast movements overshoot by 3-12% then correct
- Zero acceleration between points = bot
- Bézier curves with noise > linear interpolation

### Focus/Blur Events
- `page.fill()` skips focus/blur events
- Real browsers ALWAYS fire them
- Critical for form detection systems
- Must fire: click → focus → input → blur sequence

### Scroll Patterns
- Human scroll delta variance: 20-100px
- Bot scroll delta variance: <5px
- Humans pause to read, occasionally scroll back up
- Jump-to-element (no scroll) = bot signal

### Paste Detection
- `element.fill()` doesn't fire paste/clipboard events
- Some detection systems check if content was "typed" or "pasted"
- For realistic behavior: simulate Ctrl+V event chain for pasted content

### Honeypot Fields
- Hidden fields with CSS: `display:none`, `visibility:hidden`, `opacity:0`
- Zero-dimension or off-screen positioned fields
- Common names: `url`, `website`, `fax`, `phone2`
- Filling ANY honeypot = instant bot flag
- Must check computed CSS before filling

## Detection Systems by Platform

| Platform | Detection | Primary signals |
|----------|-----------|----------------|
| Cloudflare Turnstile | Browser environment, NOT typing/mouse | Fingerprint, JS challenges |
| DataDome | Behavioral + fingerprint | Mouse, timing, request patterns |
| reCAPTCHA v3 | Risk score from page interaction | Engagement depth, timing |
| Ashby (ATS) | Cloudflare Turnstile + form validation | Fingerprint, honeypots |
| Greenhouse (ATS) | reCAPTCHA v2/v3 | Challenge-based |
| Lever (ATS) | Basic rate limiting | Request frequency |
| Reddit | Proprietary CQS + biometric verification | Account behavior, IP, burst patterns |

## IP Reputation

- Datacenter IPs: instant flag on Reddit, high risk on Cloudflare
- Residential proxies: necessary for high-detection sites
- GeoIP must align with timezone/locale headers
- IP stability matters: rotating too fast = suspicious

## Vendor-Specific Intelligence (June 2026)

### AudioContext Fingerprinting (all vendors)

Camoufox handles AudioContext spoofing at the C++ level. Operators using
non-Camoufox setups (plain Playwright, custom CDP) must independently
verify that AudioContext oscillator output is not SwiftShader-identified.
SwiftShader audio output is a primary PerimeterX and DataDome block trigger.

### PerimeterX _px3 Token Lifecycle

The _px3 security token expires in ~60 seconds -- the most aggressive
expiry in any enterprise anti-bot system. Any pipeline navigating a
PerimeterX site across multiple pages over >60s will fail mid-session.
The `_pxvid` visitor ID cookie must persist across pages within a session.
A fresh `_pxvid` on every navigation = strong automation signal.

### WebRTC Local IP Consistency

WebRTC STUN requests can expose the client's real local IP. When using
residential proxies, ensure WebRTC is either disabled in the browser
profile or routed through the proxy. A datacenter local IP behind a
residential proxy creates a detectable inconsistency (PerimeterX, DataDome).

### reCAPTCHA v3 History Signals

- Active Google login (SID, HSID, SSID cookies) raises score +0.1 to +0.3
- _GRECAPTCHA cookie: prior successful solves accumulate reputation
- Cross-site reputation: good behavior on site A helps site B
- Profile pre-warming should include visiting Google properties before
  high-security reCAPTCHA v3 targets

### DataDome Per-Site ML Cadence

DataDome trains a separate ML model per protected site. Request cadence
is site-specific -- what's human on a news site is bot-like on a checkout.
After successful form submission or data extraction: wait 2-5s before
navigating away or closing the session.

## Calibration Datasets

| Dataset | Content | Source |
|---------|---------|--------|
| CMU Keystroke Dynamics | 51 subjects, 20K reps, hold/flight distributions | cs.cmu.edu/~keystroke |
| BlackTip | Pre-fitted calibration ranges from CMU data | github.com/rester159/blacktip |
| Balabit Mouse Dynamics | 10 users, 8 weeks mouse trajectories | github.com/balabit/Mouse-Dynamics-Challenge |
| BeCAPTCHA-Mouse | 9K trajectories, GAN-generated human paths | BiDAlab (request) |
