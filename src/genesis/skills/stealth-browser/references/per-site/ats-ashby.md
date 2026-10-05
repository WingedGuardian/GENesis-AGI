# ATS: Ashby, Greenhouse, Lever

## Ashby (high detection)

### What is known, and how
- **No Cloudflare Turnstile** on application forms (correction recorded
  2026-04-22; an earlier note claiming Turnstile was wrong).
- **Google reCAPTCHA v3 (invisible) scores the session** (live test
  2026-04-23): the user filled the form entirely by hand over VNC in Camoufox
  and the submit returned a spam error stating "we use Google's reCAPTCHA
  technology", listing VPN/proxy, ad blockers, shared networks and "unusual
  browser settings" as causes. Conclusion from that one test: the Camoufox
  environment alone earns a low score; human-like behaviour does not fix it.
- **Whether that submission reached the employer is unknown.** Treat a
  submit-time spam error as NOT submitted.
- There is no visible CAPTCHA widget and no honeypot fields on the form
  (2026-04-22 inspection). The invisible reCAPTCHA is the only pre-submit
  challenge seen.

### Other layers (from Ashby's product material as gathered 2026-04, **unsourced** here)
1. Application rate limits: employer-set "max X applications in Y days" per
   email.
2. Auto-reject rules on form answers (yes/no, dropdowns) only.
3. Post-submission fraud signals on every applicant (IP and geolocation,
   email and phone validity, device fingerprint, location mismatch, synthetic
   identity patterns). These are signals shown to recruiters next to an
   application that went through, not rejections, and the candidate is not told.
4. Optional identity verification (Socure, Incode, Persona) if the employer
   enables it.
5. AI application review: evaluation signals, not auto-reject.

So there are two different "flags": the reCAPTCHA spam error at submit blocks
the submission; a post-submit fraud signal does not.

### Fraud-signal triggers (**unsourced**)
Datacenter or VPN IP (reported as the biggest), disposable email, invalid phone,
IP location not matching the stated location, synthetic-looking resume text,
hidden text in the resume PDF, one IP submitting several identities quickly.

### Form architecture
- Company career pages often embed the form as an iframe (`<div id="ashby_embed">`
  plus a script from `jobs.ashbyhq.com`). The browser tools cannot reach inside
  it: navigate to the `jobs.ashbyhq.com/...` application URL directly
  (`browser-automation`, Selectors, Iframes).
- Submits to `api.ashbyhq.com` (`applicationForm.submit`); validation runs at
  submit, not per field.
- Resume upload is a standard `<input type="file">` (`browser_upload`).

### Strategy
- Camoufox is expected to fail reCAPTCHA v3 here. Use remote CDP (the user's
  Chrome) with the user present, or let the user submit.
- Real identity data; IP location matching the resume location.
- Screenshot before and after submit; confirm a confirmation page, not just a
  click.
- Space applications from one identity at least 5 minutes apart (**unsourced**
  interval).

## Greenhouse (medium detection; as of 2026-04, **unsourced**)

- reCAPTCHA v2 or v3, depending on the employer.
- Forms at `boards.greenhouse.io` (also embedded as iframes on company sites:
  navigate to the board URL).
- Some employers split the form into several pages.
- reCAPTCHA v3: nothing in the tools raises the score; see Ashby strategy.
- reCAPTCHA v2 checkbox or image challenge: the automated VNC path handles
  Cloudflare only. Hand the challenge to the user over VNC.
- Check for honeypots, fill visible fields, screenshot each page before Next.

## Lever (medium-low detection; as of 2026-04, **unsourced**)

- Rate limiting only; no advanced bot detection reported.
- Single-page form at `jobs.lever.co`: resume upload, optional cover letter,
  LinkedIn, website.
- Normal tool timing is enough. Space submissions from one IP at least 5
  minutes apart.
