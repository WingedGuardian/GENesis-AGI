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
