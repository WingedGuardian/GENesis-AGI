# Anti-Detection Services

External services that supplement the browser's built-in anti-detection.
Use when the built-in stealth isn't sufficient for a target site.

---

## 2Captcha — CAPTCHA Solving

Solves Cloudflare Turnstile, reCAPTCHA v2/v3, hCaptcha programmatically.

- **Cost:** $1.45/1000 solves (~$0.00145 each)
- **Speed:** 10-30s per challenge
- **API:** REST + Python SDK

```python
from twocaptcha import TwoCaptcha
solver = TwoCaptcha('API_KEY')

# Solve Turnstile
result = solver.turnstile(sitekey='0x4AAAAAAAxx', url='https://target.com/')
# result['code'] is the token — inject as cf-turnstile-response input value

# Solve reCAPTCHA v2
result = solver.recaptcha(sitekey='6Lc...', url='https://target.com/')
```

**When to use:** Sites with Turnstile/reCAPTCHA that don't auto-resolve
and human VNC intervention isn't available. Alternative to the built-in
Telegram → VNC → human escalation flow.

**Install:** `pip install 2captcha-python`

---

## Browserbase — Cloud Browser

Cloud-hosted Chromium with real hardware, residential IP, auto CAPTCHA
solving. Eliminates container fingerprint issues entirely.

- **Cost:** $99/mo startup (500 browser hours) or x402 at $0.12/hr
- **Connection:** WebSocket CDP endpoint for Playwright

```python
from browserbase import Browserbase
from playwright.async_api import async_playwright

client = Browserbase(api_key="...")
session = client.sessions.create(project_id="...")

async with async_playwright() as p:
    browser = await p.chromium.connect_over_cdp(session.connect_url)
    page = browser.contexts[0].pages[0]
    await page.goto("https://target.com")
```

**When to use:** Fallback when Camoufox in the container gets detected
(container fingerprint leaks, no GPU for WebGL). Cloud browser runs on
real hardware with genuine GPU, full font set, residential IP.

**Install:** `pip install browserbase`

---

## Residential Proxies — IP Reputation

Datacenter IPs are scored low by fraud detection systems. Residential
proxies route through real ISP connections.

### Providers

| Provider | Cost | Notes |
|----------|------|-------|
| PROXIES.SX | $4/GB shared | x402 (USDC) compatible, no signup |
| Bright Data | $8-15/GB | Largest network, enterprise |
| Oxylabs | $8-12/GB | Good API, ISP proxies |
| Webshare | $5/GB | Budget option |

**Note:** Agent Camo (agentcamo.com) is defunct — domain parked.

### Integration with Camoufox

```python
from camoufox.async_api import AsyncCamoufox

async with AsyncCamoufox(
    proxy={"server": "http://proxy.example.com:8080",
           "username": "user", "password": "pass"},
    geoip=True,  # Auto-set timezone/locale from proxy IP
) as browser:
    page = await browser.new_page()
```

**When to use:** Target site scores IP reputation heavily (Ashby,
Cloudflare-protected sites). Especially important when submitting
from a datacenter/VPS where the IP geolocates to a hosting provider.

---

## x402 Protocol (Coinbase)

HTTP 402 Payment Required protocol for agent micropayments. Some services
above support x402 for account-free, pay-per-use access via USDC on Base.

```python
pip install "x402[httpx]"
# Requires EVM wallet + USDC on Base chain
# Auto-handles 402 responses with payment signatures
```

Not required for most services — traditional API keys are simpler.
Consider x402 when you want no-account, pay-per-use access.

---

## GPU Passthrough (Hardware)

Container environments lack real GPUs, causing WebGL fingerprint
inconsistencies (software rendering vs. claimed GPU). Options:

1. **Camoufox C++ spoofing** (default, no change needed): handles 95%
   of sites by patching WebGL parameters at the engine level
2. **virtio-gpu**: Add `vga: virtio` to Proxmox VM config, pass
   `/dev/dri/` into Incus container. Mesa virgl rendering.
3. **Intel GVT-g**: If host has 6th-10th gen Intel iGPU. Virtual GPU
   with real Intel identifiers.
4. **PCIe passthrough**: Dedicated GPU card ($25-30 for GT 710).
   Perfect fingerprints.

See `references/gpu-passthrough.md` for setup instructions.

---

## CDP Remote Browser — Drive User's Real Chrome

The most reliable anti-detection approach: Genesis drives the user's actual
browser via Chrome DevTools Protocol over an SSH tunnel. Real browser = real
fingerprint = nothing to detect.

### Setup
```bash
# On user's machine: launch Chrome with debug port
google-chrome --remote-debugging-port=9222

# SSH tunnel (from Genesis container to user's machine)
ssh -L 9222:127.0.0.1:9222 user@machine
```

### Playwright connection
```python
from playwright.async_api import async_playwright

async with async_playwright() as p:
    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0]  # Use existing tab
    # or: page = await browser.contexts[0].new_page()
```

**When to use:** ATS submissions with aggressive detection (Ashby, Mercor,
any site with reCAPTCHA v3 or Fingerprint.com integration). Camoufox is
fine for non-adversarial browsing.

**Status:** Not yet integrated into browser.py. Planned as Step 2 of
stealth Layer 3.

---

## Anti-Detection Landscape (researched 2026-04-23)

### Camoufox limitations (confirmed)
- 100% detected by reCAPTCHA v3 (GitHub Issue #284)
- Detected by Akamai (Issue #555), Google (Issue #388)
- Container detection (Issue #311) — not just containers, bare metal too
- Viewport mismatch bug: JS reports 1920px while Playwright is 1280px
- Maintenance stalled ~1 year, forks exist but degraded

### No open-source tool bypasses reCAPTCHA v3
A benchmark (techinz/browsers-benchmark) tested ALL major tools — they
ALL score 0.9 on test sites (including stock Playwright). Production
sites use additional signals beyond v3 scoring.

Community consensus: use external CAPTCHA solvers when challenges appear,
don't try to bypass the scoring itself.

### Tools evaluated (April 2026)
| Tool | Verdict |
|------|---------|
| CloakBrowser | Unverified 0.9 claim (single screenshot). Worth testing. |
| Pydoll | WebDriver-free CDP. No reCAPTCHA claims. Behavioral only. |
| Browser Use | LLM agent framework. Zero anti-detection. Cloud version adds stealth ($). |
| Stagehand | TypeScript, Browserbase-oriented. Not useful for Genesis. |
| rebrowser-patches | Fixes CDP leak only. Doesn't address reCAPTCHA. |
| undetected-chromedriver | Still detected by v3 invisible (Issue #2280, Nov 2025). |

### What actually matters for detection
1. **IP reputation** (residential > datacenter) — #1 signal
2. **Browser fingerprint** (real browser > any patched browser)
3. **Behavioral realism** (we already have this via Layer 2)
4. **CAPTCHA solver as safety net** (CapSolver ~$1/1K solves)
