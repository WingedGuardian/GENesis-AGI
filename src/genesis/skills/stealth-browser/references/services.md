# Anti-Detection Services

External services that supplement the browser's own anti-detection. None of
the third-party ones below is installed or wired into Genesis; the code here
is a sketch for a future integration.

**Every paid use needs the user's explicit approval, each time.** Prior
approval does not carry over. Paying through a crypto wallet (x402) is a
financial transaction under the same rule. API keys go in `secrets.env` or
`reference_store`, never in code or chat.

Prices are as read on the date shown; recheck before quoting them.

---

## TinyFish (wired in)

- `browser_navigate(url, tinyfish=True)`: cloud Chromium over CDP, 1 credit per
  4 minutes (tool docstring, 2026-10-04). No tool ends the session early; it
  ends at idle cleanup or session end. See `browser-automation`, Layer 4.
- `web_agent(url, goal)`: goal-driven agent, about $0.015 per step
  (tool docstring). The daily budget is checked and only logged, never
  enforced; the default `max_steps=100` is about $1.50 per call, so pass a
  small `max_steps`.
- `web_fetch(backend="auto")` also tries TinyFish first for plain reads.

---

## 2Captcha (CAPTCHA solving)

- **Price (2captcha.com/pricing, read 2026-10-04), per 1,000 solves:** Cloudflare
  Turnstile $1.45; reCAPTCHA v2 $1-2.99; reCAPTCHA v3 $1.45 (score <= 0.3) to
  $2.99 (score > 0.3).
- **Speed:** 10-30 s per challenge (**unsourced**).
- **Standalone Turnstile widget:** send `sitekey` and page URL; put the returned
  token in `cf-turnstile-response`.
- **Cloudflare challenge page (interstitial):** also needs `action`, `cData` and
  `chlPageData`, captured by intercepting `turnstile.render` before the widget
  loads, and the page must use the `userAgent` the API returns (2Captcha docs,
  "Cloudflare Turnstile", read 2026-10-04). Without those the token is useless.

```python
from twocaptcha import TwoCaptcha  # pip install 2captcha-python (not a Genesis dependency)
solver = TwoCaptcha(api_key)
result = solver.turnstile(sitekey="0x4AAAAAAA...", url="https://target.example/")
# result["code"] is the token
```

Injecting a token means writing a hidden input with `browser_run_js`, which is
logged and is an untrusted script action; prefer handing the challenge to the
user, which depends on the layer (`browser-automation`, Safety gates, Hand-off).

---

## Browserbase (cloud browser)

Cloud Chromium with captcha solving on paid plans; connect with Playwright's
`connect_over_cdp`.

**Plans (browserbase.com/pricing, read 2026-10-04):**

| Plan | Included | Then |
|---|---|---|
| Free | 1 browser hour, 3 concurrent, 15 min/session, no captcha solving | n/a |
| Developer | 100 browser hours, 25 concurrent, captcha solving, basic stealth | $0.12/browser hour |
| Startup | 500 browser hours, 100 concurrent, captcha solving, basic stealth | $0.10/browser hour |
| Enterprise | custom, advanced stealth | usage-based |

The fetched page text did not show the plans' base monthly fees; read them on
the page. Earlier notes quoting "$0.002/session" or x402 at $0.12/h were wrong
or are no longer listed.

```python
from browserbase import Browserbase          # pip install browserbase
from playwright.async_api import async_playwright

session = Browserbase(api_key=api_key).sessions.create(project_id=project_id)
async with async_playwright() as p:
    browser = await p.chromium.connect_over_cdp(session.connect_url)
    page = browser.contexts[0].pages[0]
```

Use when the container's environment itself is detected (no GPU, container
fingerprint) and the user approves the spend.

---

## Residential proxies (IP reputation)

Prices as noted 2026-04, **unverified**:

| Provider | Cost | Notes |
|---|---|---|
| PROXIES.SX | $4/GB shared | x402 (USDC) payments |
| Bright Data | $8-15/GB | largest network |
| Oxylabs | $8-12/GB | ISP proxies |
| Webshare | $5/GB | budget |

Agent Camo (agentcamo.com) was defunct as of 2026-04 (domain parked).

Camoufox takes a proxy at launch, and `geoip=True` aligns timezone and locale
with the proxy IP. Genesis's launch code passes no proxy today, so this needs a
code change, not a skill-level action:

```python
AsyncCamoufox(proxy={"server": "http://proxy.example:8080",
                     "username": "...", "password": "..."},  # noqa: placeholder
              geoip=True)
```

---

## x402 protocol (Coinbase)

HTTP 402 micropayments in USDC on Base, for no-account, pay-per-use access.
Every x402 payment is a financial transaction: explicit user approval per
payment, no standing wallet authority.

```bash
pip install "x402[httpx]"   # needs an EVM wallet holding USDC on Base
```

---

## GPU passthrough (hardware)

Containers have no real GPU, so WebGL reports software rendering. Options:
1. Camoufox engine-level WebGL spoofing (default; "handles most sites" is
   **unsourced**).
2. virtio-gpu: give the VM a virtio GPU and pass `/dev/dri/` into the container
   (Mesa virgl).
3. Intel GVT-g on 6th-10th gen Intel iGPUs.
4. PCIe passthrough of a dedicated card.

Setup is host-specific; follow the hypervisor's and container runtime's docs.

---

## Remote CDP (the user's Chrome)

Integrated: `browser_navigate(url, remote=True)`. Setup, security and behaviour
live in one place: `browser-automation`, "Remote CDP setup".

---

## Landscape (researched 2026-04-23, not re-run since)

### Camoufox against production scoring
- Detected by reCAPTCHA v3 in production (Camoufox GitHub issue #284), Akamai
  (#555) and Google (#388); container-and-bare-metal detection (#311). Issue
  numbers as read 2026-04-23.
- A public benchmark (techinz/browsers-benchmark) found every tool tested,
  including stock Playwright, scored 0.9 on reCAPTCHA v3 test pages. Both are
  true: demo pages score generously, production deployments add signals (site
  configuration, history, IP). Judge by the target site, not a demo page.

### Tools evaluated (April 2026)
| Tool | Verdict |
|---|---|
| patchright | Playwright fork that hides the `Runtime.enable` CDP signal; candidate for the Chromium fallback (2026-10-04) |
| CloakBrowser | unverified 0.9 claim (one screenshot) |
| Pydoll | WebDriver-free CDP, behavioural only |
| Browser Use | agent framework, no anti-detection (tracked separately, #2666) |
| Stagehand | TypeScript, Browserbase-oriented |
| rebrowser-patches | fixes the CDP leak only; stale since 2025-05 |
| undetected-chromedriver | still detected by v3 invisible (its issue #2280, Nov 2025) |

### What matters most (community consensus, **unsourced** ranking)
1. IP reputation (residential over datacenter).
2. Browser fingerprint (a real browser over any patched one).
3. Behavioural realism (the tools' timing and typing).
4. A CAPTCHA solver as a paid safety net (CapSolver about $1 per 1,000,
   **unverified**).
