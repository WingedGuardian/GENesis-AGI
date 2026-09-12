# Web Tools — Decision Guide

Genesis has multiple web tools across two execution contexts.


## Canonical Interface (MCP — all session types)

These are the PRIMARY tools. Use them by default in all contexts.

| Need | Tool | Notes |
|------|------|-------|
| Fetch URL content | `web_fetch(url)` | Anti-bot, JS fallback, structured output |
| Search the web | `web_search(query)` | SearXNG unlimited, structured results |
| AI-summarized fetch | CC `WebFetch` | Foreground only — when you need AI summary |
| Quick general lookup | CC `WebSearch` | Foreground only — simple questions |
| JS-heavy SPA | `web_fetch(url, backend="crawl4ai")` | Playwright rendering |
| Semantic search | `web_search(query, backend="exa")` | Find similar by meaning |
| Synthesized answer | `web_search(query, backend="perplexity")` | Multi-source synthesis |
| Page interaction | `browser_navigate` + `browser_click` | Login, forms, visual |

**Default rule:** `web_fetch`/`web_search` first. CC tools for AI summaries only.
Browser for interaction. ATS APIs for job listings.

---
## Search — "I need to find something"

| Tool | Context | Use when... | Free tier |
|------|---------|-------------|-----------|
| **CC WebSearch** | CC sessions | Quick reliable search, general queries | Included |
| **SearXNG** (`localhost:55510`) | Both | Structured JSON, `site:` filters, bulk queries | Unlimited (self-hosted) |
| **Tavily** (API) | Both | AI-optimized results for agent pipelines | 1,000/month |
| **Exa** (API) | Both | Neural/semantic search, conceptual discovery | 1,000/month |
| **Perplexity** (API) | Both | Synthesized answers with citations | None (paid only) |
| **Brave** (API) | Genesis runtime | Auto-fallback when SearXNG fails | ~1,000/month |

**Default:** use the Genesis `web_search` MCP interface. Select a backend only
when the task needs it; the tool's automatic chain handles ordinary fallback.
Foreground-only CC tools remain useful for a quick lookup or AI-processed fetch.

## GitHub Search — "I need to find repos, code, or libraries"

When searching for open-source projects, implementation patterns, or
libraries on GitHub, use these INSTEAD of generic web search:

| Tool | Context | Use when... |
|------|---------|-------------|
| **`recon_github_search`** | Genesis research sessions | Find public GitHub.com repositories or issues through an unauthenticated fixed endpoint |
| **`recon_github_read`** | Genesis research sessions | Inspect public GitHub.com repository metadata, bounded trees, or UTF-8 source files up to Genesis's 8 MiB file limit |
| **`gh search repos/code`** | Foreground with Bash | Direct CLI fallback |
| **grep.app** | Both | `web_fetch("https://grep.app/search?q=QUERY")` — semantic code search, better than GitHub native |
| **`gh api search/repositories?q=QUERY`** | Foreground with Bash | Structured CLI fallback |
| **Exa** with GitHub filter | Both | `web_search(query, backend="exa")` with `include_domains: ["github.com"]` |

**When to use:** Any task involving "search GitHub," "find a library,"
"how do other projects handle X," or "what open-source tools exist for Y."
Generic web search returns blog posts ABOUT GitHub projects; these tools
search GitHub directly. grep.app is especially valuable for finding
implementation patterns across repos. GitHub's code-search API requires
credentials, so the public-only recon tool discovers candidate repositories and
then inspects their trees/files; foreground sessions can use authenticated CLI
search when operator-private visibility is appropriate.

---

## Fetch — "I have a URL, get the content"

| Tool | Context | Use when... |
|------|---------|-------------|
| **Crawl4AI** | Both | JS-rendered pages, free, local, no rate limits |
| **Scrapling** (WebFetcher) | Genesis runtime | Simple HTTP pages, TLS fingerprint anti-bot |
| **Cloudflare Browser** | Both | JS rendering escalation (if API key set) |
| **CC WebFetch** | CC sessions | Quick fetch + AI summarization |
| **Firecrawl** (API) | CC sessions | Complex pages, paywall bypass (costs credits) |

**Default:** `web_fetch` uses the maintained automatic fetch chain. Select a
specific backend only for a demonstrated need. Firecrawl is paid and explicit.

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

- `src/genesis/providers/tavily_adapter.py` — TavilyAdapter
- `src/genesis/providers/exa_adapter.py` — ExaAdapter
- `src/genesis/providers/crawl4ai_adapter.py` — Crawl4AIAdapter
- `src/genesis/providers/cloudflare_crawl.py` — CloudflareCrawlAdapter
- `src/genesis/research/web_adapter.py` — WebSearchAdapter (SearXNG+Brave)
- `src/genesis/research/perplexity.py` — PerplexityAdapter
- `src/genesis/web/fetch.py` — WebFetcher (Scrapling+httpx)
- `src/genesis/web/search.py` — WebSearcher (SearXNG client)
- `src/genesis/providers/registry.py` — ProviderRegistry
