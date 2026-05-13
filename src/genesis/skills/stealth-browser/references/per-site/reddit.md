# Reddit

## Detection System (as of 2026-04)

Reddit uses **proprietary internal detection**, not an external WAF.

### Contributor Quality Score (CQS)
Invisible 5-tier score based on:
- Account age
- IP stability
- Karma (post + comment)
- Engagement quality (replies, upvotes received)
- Rule adherence (reports, removals)

Low CQS = content auto-removed before any human sees it.

### Biometric Verification (March 2026)
- Flagged accounts must verify via passkeys, biometrics, or government ID
- **Cannot be automated** — this is the hard wall
- Prevention (never getting flagged) is the only strategy

### ~100,000 bot accounts removed daily

## What Gets You Caught (ranked by risk)

1. Standard Playwright/Selenium without stealth patches (instant)
2. Datacenter IPs (instant)
3. Burst activity patterns — most bans from bursts, not total volume
4. Same content across multiple subreddits (shadowban)
5. AI-generated polished content (detected faster than casual writing)
6. New accounts acting too quickly without karma/age

## Rate Limits

| Action | New account | Established |
|--------|------------|-------------|
| Comments | 2-3/day | Higher, varies |
| Posts | None for ~2 weeks | Varies by subreddit |
| Subreddit with low karma | 1 per 10 min | Normal |
| API (OAuth) | 100 req/min | 100 req/min |
| API (unauth) | 10 req/min | 10 req/min |
| Browser scraping | ~50 pages before flagged | Similar |
| DMs | <15 per 5 min | <15 per 5 min |

## Strategy

- **Residential proxy mandatory** — datacenter IPs are instant flags
- **Account age matters** — don't use new accounts for automation
- **No burst patterns** — space actions over hours, not minutes
- **Content quality** — casual, human-like. Overly polished = suspicious
- **IP stability** — don't rotate IPs within a session
- **GeoIP alignment** — IP location must match account's typical location
- **Self-service API keys eliminated** (Nov 2025) — all OAuth requires
  manual Reddit pre-approval

## Bottom Line

Reddit automation is HIGH RISK. The biometric verification wall means
there's no recovery from being flagged. Every interaction must be
designed to never trigger the first flag. Use with extreme caution.
