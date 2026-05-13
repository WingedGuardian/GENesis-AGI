# ATS: Ashby, Greenhouse, Lever

## Ashby (Medium-High detection)

**CORRECTION (2026-04-22):** Ashby does NOT use Cloudflare Turnstile on
application forms. Previous research was incorrect.

**UPDATE (2026-04-23):** Ashby DOES use Google reCAPTCHA v3 (invisible).
Confirmed by live test — user filled form entirely manually via VNC through
Camoufox, still flagged as spam. Error message explicitly states "we use
Google's reCAPTCHA technology." The reCAPTCHA scores the browser environment
throughout the session, not just at submit. Camoufox fingerprint alone
triggers a low score regardless of human-like behavior.

### Actual Detection Stack

Ashby uses **six layers of detection** (reCAPTCHA is pre-submission, rest mostly post):

0. **Google reCAPTCHA v3** (pre-submission, invisible): Scores the browser
   session continuously. Camoufox triggers a low score based on fingerprint
   alone — confirmed by manual VNC test (2026-04-23). Error message lists:
   VPN/proxy, ad blockers, shared networks, "unusual browser settings."

1. **Application Rate Limits** (pre-submission): per-email frequency caps.
   Employer-configurable "max X applications in Y days." A new email resets.

2. **Auto-Reject Rules** (at submission): based solely on form answer values
   (yes/no, dropdown). Does NOT use email, IP, or behavioral signals.

3. **Built-in Fraud Detection** (post-submission, automatic, Sept 2025):
   Runs on ALL applicants. Analyzes: IP/geolocation, email validity, phone
   risk, device/browser fingerprint, connection method, location mismatches,
   synthetic identity patterns. Provides **signals, not verdicts** — the
   application goes through and recruiters see flags alongside it.
   **Candidates never know they've been flagged.**

4. **Third-Party Verification** (optional): Socure, Incode, Persona
   integrations. Auto-run on submission if employer enables them.

5. **AI Application Review** (content analysis): evaluates resume vs. JD.
   Not auto-reject — evaluation signals only.

### What Triggers Fraud Flags

- Datacenter/VPN IP (biggest signal)
- Disposable email (mailinator, guerrillamail, etc.)
- Invalid phone number
- IP geolocation doesn't match stated location
- Synthetic identity patterns (AI-optimized resume text)
- White/invisible text in resume PDF
- Same IP + different identities in rapid succession

### What Does NOT Trigger Issues

- Real personal email with history
- Residential IP matching stated location
- Genuine resume content
- Reasonable application frequency
- Standard browser with proper fingerprint (Camoufox handles this)

### Form Architecture

- Iframe embed: `<div id="ashby_embed">` + script from `jobs.ashbyhq.com`
- API: `POST api.ashbyhq.com/applicationForm.submit`
- No honeypot fields (confirmed)
- No CAPTCHA of any kind on the form itself
- Submit-time validation only (not per-field)
- File upload via standard `<input type="file">`

### Strategy

- Use real identity data (name, email, phone, LinkedIn)
- Ensure IP geolocation matches resume location
- Residential proxy if submitting from datacenter
- Per-keystroke typing for device fingerprint legitimacy
- Pre-warm by browsing company's public page first
- Screenshot before/after submit for audit trail
- Space applications 5+ minutes apart from same identity

### "Flagged as Spam" = Not a Rejection

Applications flagged by fraud detection **still go through**. Recruiters
see fraud signals alongside the application. There is no auto-rejection.
There is no candidate-facing notification of being flagged. If the
application content is strong, the recruiter may ignore the fraud flag.

---

## Greenhouse (Medium detection)

- Uses **reCAPTCHA v2 or v3** depending on employer config
- Form hosted at `boards.greenhouse.io`
- Standard field layout with optional custom questions
- Multi-page forms (some employers split into steps)
- File upload for resume, optional cover letter
- **reCAPTCHA v3**: invisible, scores interaction quality
- **reCAPTCHA v2**: checkbox challenge, may need VNC collaborate mode

### Strategy
- reCAPTCHA v3: ensure sufficient page interaction before submit
- reCAPTCHA v2: if checkbox appears, try clicking it; if image
  challenge triggers, fall back to VNC (always available)
- Fill all visible fields, skip honeypots
- Multi-page: navigate each page, fill, screenshot, next

## Lever (Medium-Low detection)

- Basic rate limiting, no advanced bot detection
- Form hosted at `jobs.lever.co`
- Simple single-page form
- File upload for resume
- Optional fields: cover letter text, LinkedIn, website

### Strategy
- Lowest detection risk of the three
- Standard human-like timing is sufficient
- Rate limit: don't submit multiple applications in quick succession
  from the same IP (space 5+ minutes apart)
