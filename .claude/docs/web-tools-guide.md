# Web Tools — Decision Guide

Genesis has multiple web tools across two execution contexts.


## Canonical Interface (MCP — all session types)

These are the PRIMARY tools. Use them by default in all contexts.

| Need | Tool | Notes |
|------|------|-------|
| Fetch URL content | `web_fetch(url)` | Anti-bot, JS fallback, structured output |
| YouTube video (what it says) | `web_fetch(url)` | Metadata + transcript via yt-dlp (captions in the video's language; no audio transcription), wrapped as untrusted content; no Bash needed. `config/youtube_fetch.yaml` sets certificate handling |
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

### Optional: answer CC `WebSearch` with Genesis (`genesis-web-override`)

An opt-in Claude Code plugin in `plugins/genesis-web-override/` intercepts the
built-in `WebSearch`, main session and subagents alike, and answers it with the
Genesis `web_search` chain. It falls back to the built-in on any failure: Genesis
down, the MCP server not connected, the call refused by the session's permission
mode, an error, or zero results. It also falls back on any call that sets
`allowed_domains` or `blocked_domains`, which the Genesis chain honours on one
backend only. `WebFetch` is not touched: Claude Code leaves cross-host redirects
to the model on purpose.

**Turn it on for interactive slots:** add `GENESIS_CC_WEB_OVERRIDE=1` to
`~/.genesis/cc-slot.env`. Each slot created after that gets
`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` (Claude Code's early-access function hooks)
and loads the plugin from this checkout with `--plugin-dir`, so a `git pull`
reaches it at the next slot start and nothing outside a slot loads it. Remove the
line to turn it off: new slots then unset the flag. An existing slot keeps what it
started with until it is recreated, and relaunching `claude` by hand inside a slot
runs without the plugin.

Dispatched sessions never run it. These all pin
`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=0` (`src/genesis/cc/child_env.py`), CCInvoker
at its last launch gate, after every env merge:

- CCInvoker;
- the headless judge;
- the experimentation router;
- the dashboard's update sessions;
- the guardian's recovery session on the host;
- remote sessions started over SSH. Claude Code
resolves the flag as the env var if set, otherwise a server-side default, so
unset is not the same as off. Do not put the flag in a `settings.json` `env`
block: Claude Code applies that over the inherited environment, including a
dispatched session's.

What an answered call skips, because the plugin answers instead of the built-in:
the built-in's own permission check and its PreToolUse hooks (the advisory
`web_tools_gate.py` nudge), and its per-session search cap. The permission check
runs on `mcp__genesis-health__web_search` instead, since the plugin calls it
through the normal tool pipeline. The flag is not specific to this plugin: it
turns on hook modules for every enabled plugin that ships them.

**Another plugin can make it fall back on most searches.** The plugin calls
`web_search` through Claude Code's normal tool pipeline, so PostToolUse hooks
from other plugins run on that result too. A hook that rewrites large MCP
results (for example, one that archives them to disk and returns a summary
instead) leaves the plugin text that is not JSON, and those searches fall
back to the built-in. token-optimizer does this for results of 4096 characters
or more, which a typical 10-result search exceeds, so smaller searches still
succeed. The plugin then logs "a PostToolUse hook may have rewritten the
web_search result", which shows in the session and in a `--debug-file` log.
Exempt `mcp__genesis-health__web_search` from that hook. For token-optimizer,
add it to `TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS` in the `env` block of
`~/.claude/settings.json` (a comma-separated list: append it if the variable is
already set). The cost is that every session then gets the full `web_search`
result instead of the summary.

Two limits. Claude Code checks the answer against `WebSearch`'s output schema
after the plugin returns, so a shape a later Claude Code stops accepting reaches
the model as a tool error rather than the built-in search: re-checked on every pin
bump (`docs/reference/cc-compatibility.md`, update checklist). And the Genesis call
has no timer of its own; a stalled `web_search` stalls the search for as long as
the MCP call runs.

## GitHub Search — "I need to find repos, code, or libraries"

When searching for open-source projects, implementation patterns, or
libraries on GitHub, use these INSTEAD of generic web search:

| Tool | Context | Use when... |
|------|---------|-------------|
| **`recon_github_search`** | Genesis research sessions | Find public GitHub.com repositories, or issues through literal text plus structured repository/state/label filters, using an unauthenticated fixed endpoint |
| **`recon_github_read`** | Genesis research sessions | Inspect public GitHub.com repository metadata, bounded trees, or UTF-8 source files up to Genesis's 8 MiB file limit |
| **`gh search repos/code`** | Foreground with Bash | Direct CLI fallback |
| **grep.app** | Foreground only | `searchGitHub` via the `grep-app` MCP server (`https://mcp.grep.app`, no API key) — LITERAL/regex code search over ~1M public repos. NOT available to background research sessions: `_MCP_PROFILES["research"]` grants only health/memory/recon, and `strict_mcp_config` makes that list authoritative, so user-scope servers are dropped. **Nothing replaces it there** — `Bash` is denied too, so `gh search code` is out, and `recon_github_search` does repositories and issues, not code. Run exact-code search from a foreground session; #2329 tracks closing the gap. |
| **`gh api search/repositories?q=QUERY`** | Foreground with Bash | Structured CLI fallback |
| **Exa** with GitHub filter | Both | `web_search(query, backend="exa")` with `include_domains: ["github.com"]` |

**When to use:** Any task involving "search GitHub," "find a library,"
"how do other projects handle X," or "what open-source tools exist for Y."
Generic web search returns blog posts ABOUT GitHub projects; these tools
search GitHub directly. grep.app is especially valuable for finding
implementation patterns across repos — but query it with actual code
(`useState(`, `max_review_iterations`, `(?s)try {.*await`), never with keywords
or a question. It greps; it does not embed. For conceptual discovery where you
cannot name the code, use Exa instead. GitHub's code-search API requires
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
