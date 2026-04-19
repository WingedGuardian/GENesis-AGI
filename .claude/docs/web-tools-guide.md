# Web Tools — Decision Guide

Genesis has multiple web tools across two execution contexts.

## Search — "I need to find something"

| Tool | Context | Use when... |
|------|---------|-------------|
| **CC WebSearch** | CC sessions | Quick reliable search, current events, general queries |
| **SearXNG** (`localhost:55510`) | Both | Structured JSON, `site:` filters, bulk/batch queries |
| **Perplexity** (API) | Genesis runtime | Synthesized answer with citations (if key configured) |
| **Brave** (API) | Genesis runtime | Auto-fallback when SearXNG fails (in WebSearchAdapter) |

**CC sessions default:** CC `WebSearch` for general lookups. SearXNG via
Bash (`curl localhost:55510/search?q=...&format=json`) when you need
structured JSON, `site:` filtering, or batch queries.

## Fetch — "I have a URL, get the content"

| Tool | Context | Use when... |
|------|---------|-------------|
| **Crawl4AI** | Both | JS-rendered pages, free, local, no rate limits |
| **Scrapling** (WebFetcher) | Genesis runtime | Simple HTTP pages, TLS fingerprint anti-bot |
| **Cloudflare Browser** | Both | JS rendering escalation (if API key set) |
| **CC WebFetch** | CC sessions | Quick fetch + AI summarization |
| **Firecrawl** (API) | CC sessions | Complex pages, paywall bypass (costs credits) |

**CC sessions default:** Crawl4AI first (free, local, JS-capable).
CC `WebFetch` for AI-processed summaries. Firecrawl as last resort.

## Browser — "I need to interact with a page"

| Tool | Context | Use when... |
|------|---------|-------------|
| **browser_navigate/click/fill** | CC sessions | Login flows, form filling, visual verification |
| **Playwright** (direct via Bash) | CC sessions | Complex browser automation, screenshots |

## ATS Job APIs — "I need job listings"

| API | Endpoint |
|-----|----------|
| **Greenhouse** | `boards-api.greenhouse.io/v1/boards/{slug}/jobs` |
| **Ashby** | `api.ashbyhq.com/posting-api/job-board/{slug}` |
| **Lever** | `api.lever.co/v0/postings/{slug}` |

Always try ATS APIs first (free, structured). Scrape only for companies
not on these platforms.

## Key Files

- `src/genesis/providers/crawl4ai_adapter.py` — Crawl4AIAdapter
- `src/genesis/providers/cloudflare_crawl.py` — CloudflareCrawlAdapter
- `src/genesis/research/web_adapter.py` — WebSearchAdapter (SearXNG+Brave)
- `src/genesis/research/perplexity.py` — PerplexityAdapter
- `src/genesis/web/fetch.py` — WebFetcher (Scrapling+httpx)
- `src/genesis/web/search.py` — WebSearcher (SearXNG client)
- `src/genesis/providers/registry.py` — ProviderRegistry
