# Reddit

Gathered 2026-04 from public reporting and community sources. Nothing here was
measured by Genesis; every number is **unsourced** unless a source is named.
Recheck before relying on a limit.

## Detection

Reddit uses in-house detection, not an external WAF.

### Contributor Quality Score (CQS)
A hidden 5-tier score built from account age, IP stability, karma, engagement
quality and rule adherence. Low CQS reportedly gets content removed before a
human sees it.

### Verification wall (reported March 2026)
Flagged accounts must verify with a passkey, biometrics or government ID. That
cannot be automated, so the only strategy is never getting flagged.

Reddit is reported to remove about 100,000 bot accounts a day.

## What gets accounts caught (reported, ranked by risk)

1. Plain Playwright/Selenium without stealth patches (immediate).
2. Datacenter IPs (immediate).
3. Burst activity; most bans are reported to come from bursts, not volume.
4. The same content across several subreddits (shadowban).
5. Polished, AI-sounding content.
6. New accounts acting fast with little karma or age.

## Reported limits

| Action | New account | Established |
|---|---|---|
| Comments | 2-3/day | higher, varies |
| Posts | none for ~2 weeks | varies by subreddit |
| Low-karma posting in a subreddit | 1 per 10 min | normal |
| API, OAuth | 100 requests/min | 100 requests/min |
| API, unauthenticated | 10 requests/min | 10 requests/min |
| Browser scraping | ~50 pages before a flag | similar |
| DMs | under 15 per 5 min | under 15 per 5 min |

Self-service API keys were reportedly withdrawn in November 2025; OAuth access
now needs Reddit's manual approval.

## Strategy

- Use an aged account; never automate a new one.
- Residential IP, stable within a session, located where the account usually is.
- No bursts: space actions over hours, not minutes.
- Casual, specific writing; never cross-post identical text.

## Bottom line

High risk. A flag leads to a verification wall with no automated way back, so
every interaction has to avoid the first flag.
