# Evaluation Action Items — 2026-03-17

> **Note:** The canonical location for action items is now `docs/actions/`.
> New items should go to `docs/actions/genesis/active.md` (Genesis dev) or
> `docs/actions/user/active.md` (user personal). This document is retained
> as a historical reference. Items here will be consolidated during a
> separate triage pass.

Living document capturing action items from ongoing technology evaluation
sessions. Items are added during evaluation, then planned and executed
separately.

---

## Quick Wins (Do Soon)

### QW-1: Matt Pocock Skills Adoption
**Source:** Video 5 — "5 Claude Code skills I use every single day"
**What:** Study and adopt patterns from [mattpocock/skills](https://github.com/mattpocock/skills).
Key skills to evaluate for Genesis CC workflows:
- **grill-me** — self-interrogation before plan execution
- **triage-issue** — bug analysis → root cause → TDD fix
- **improve-codebase-architecture** — architectural review
- **write-a-prd** / **prd-to-issues** — PRD → vertical-slice issues
**Scope:** V3 (immediate)
**Priority:** High — low effort, high leverage

### QW-2: OpenClaw Use Case Quick Wins
**Source:** Video 2 — "Top 10 OpenClaw Use Cases"
**What:** Review [awesome-openclaw-usecases](https://github.com/hesamsheikh/awesome-openclaw-usecases)
and [Forward Future 25+ use cases](https://forwardfuture.ai/p/what-people-are-actually-doing-with-openclaw-25-use-cases)
for patterns Genesis can adopt immediately with existing infrastructure.
Focus areas:
- Morning briefing enhancements (already have morning report — can we make it richer?)
- Multi-agent orchestration patterns (CC session dispatch improvements)
- STATE.yaml pattern for subagent coordination
**Scope:** V3 (immediate)
**Priority:** Medium

### QW-3: Idempotent Outreach Sends
**Source:** Video 3 — AgentMail evaluation
**What:** Ensure awareness loop retries don't double-send alerts/reports.
Dedup key on (alert_type, subject_hash, time_window).
**Scope:** V3 (immediate)
**Priority:** Medium

### QW-4: Update Skill Conventions with Workflow Patterns
**Source:** Video 8 — "Most People Build Claude Skills Wrong"
**What:** Update `docs/reference/genesis-skill-conventions.md` with the 5
canonical workflow patterns from Anthropic's guide:
1. Sequential workflow orchestration
2. Multi-MCP coordination
3. Context-aware tool routing
4. Iterative refinement (quality gate loop)
5. Domain-specific knowledge
Each Genesis skill should explicitly identify which pattern it follows.
Add the quality gate pattern (AI quality check, threshold, max iterations)
as a standard building block for autonomous CC sessions.
**Scope:** V3 (immediate)
**Priority:** High — directly improves all future skill writing

### QW-5: Verify FastMCP Serialization Overhead
**Source:** NVIDIA NeMo Retriever — found MCP to be a performance bottleneck
**What:** Our FastMCP is in-process (not a separate server), so we likely don't
have NVIDIA's problem. But verify: profile a typical memory_recall or
procedure_recall call path and confirm there's no unnecessary JSON serialization
round-trip. Just verification, not a migration — we stay MCP-native.
**Scope:** V3 (immediate)
**Priority:** Low — likely fine, but worth confirming

### QW-6: Cloudflare /crawl as Web Provider
**Source:** Video 6 — Cloudflare /crawl API
**What:** Add Cloudflare /crawl endpoint as a provider in `genesis.web`
alongside SearXNG and Brave. Key capability: whole-site crawling to Markdown.
`render: false` mode is free during beta. Useful for:
- Recon pipeline (P-5) external reference crawling
- Documentation ingestion for learning new frameworks
- RAG knowledge base building
**Needs:** Cloudflare account + API token (may already have one).
**Scope:** V3 (immediate)
**Priority:** Medium — free tier is useful now, complements existing providers

### QW-7: Prompt Injection Defense for Ingested Content
**Source:** Google ADK + Antigravity video — Model Armor callbacks
**What:** The threat is NOT the user — the user should be able to tell Genesis
anything. The threat is third-party content Genesis processes: web pages scraped
by recon, documents from inbox, email content, search results, transcripts. Any
of these could contain injected instructions designed to hijack Genesis's behavior.
Google's Model Armor pattern: intercept content before it reaches the model,
detect injection patterns, sanitize or flag.
**Implementation:** Add content sanitization to ingestion points:
- Recon pipeline (web content before analysis)
- Inbox evaluator (email content before processing)
- WebFetch results (before being injected into context)
- Any external content that enters Genesis's context
NOT on the user's direct messages — those are sovereign.
**Scope:** V3 (immediate — real security gap)
**Priority:** High — defense-in-depth at the content boundary

### QW-8: Mine Skill Repos for Patterns (+ Recon Target)
**Source:** Sabrina Ramonov "5 GitHub Repos" video
**What:** Study community skill repos for pattern insights:
- [awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills)
- [Superpowers](https://github.com/obra/superpowers) (already installed)
- [awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills) (500+)
- Anthropic's official skill repo
Focus: multi-MCP coordination patterns, iterative refinement, quality gates.
Feeds QW-4 (skill conventions update).

**Also a recon system target:** These repos should be added to the SEER/recon
watchlist for proactive monitoring. When new skills are added to these repos
that match Genesis's capabilities, recon should flag them. V4 goal: Genesis
autonomously evaluates and pulls good patterns into its own codebase.
**Scope:** V3 (study), V4 (autonomous skill adoption via recon)
**Priority:** Medium — informs skill convention improvements

### QW-9: TDD for Security Hooks + Agent Identity
**Source:** Google ADK video — test-driven security, agent identity principle
**What:** Two related items:
1. **Security hook TDD** — Write tests that verify our hooks block what they
   should: injection attempts, privilege escalation, protected path access.
   Currently untested — we trust without proof. Google's approach: red (write
   failing test for attack), green (implement defense), refactor.
2. **Agent identity at call sites** — Google's "agent identity" principle:
   constrain what an agent can/can't do per call site. Genesis's router has
   call_site_id on every LLM call. We should be using this to enforce
   per-call-site identity constraints (e.g., skill_refiner can't do X,
   procedure_extractor can't do Y). Check which call sites have identity
   constraints and which don't.

Also informs V4 containerized runtime testing: proposed changes run in a nested
container before applying to production. TDD for security is the foundation.
**Scope:** V3 (immediate)
**Priority:** Medium-High — verify before trusting

**Completed (2026-03-18):**

**Security TDD:** 272 tests across 4 test files in `tests/test_hooks/`:
- `test_pretool_check.py` (76 tests) — CRITICAL path blocking, fallback, glob patterns
- `test_behavioral_linter.py` (56 tests) — no-hide-problems + no-unguarded-kill rules, escape hatches
- `test_procedure_advisor.py` (45 tests) — trigger matching, field extraction, output format
- `test_inline_hooks.py` (95 tests) — Bash destructive commands + WebFetch YouTube blocking

**Bugs found:** (1) pretool_check.py crashes on empty YAML config (AttributeError not caught).
(2) `youtu.be` short URLs don't trigger youtube procedure advisor (config gap).
(3) `34_procedure_extraction` call site missing from `model_routing.yaml` — extractor is silently broken.

**Agent Identity Audit:** Full audit of 17+ call sites. Key findings:
- **CRITICAL:** `30_triage_calibration` writes to filesystem with no identity constraints;
  `33_skill_refiner` self-validates (propose + validate same call site/model);
  `34_procedure_extraction` not in YAML (silently broken).
- **HIGH:** CC Inbox evaluation has full tool access (no `disallowed_tools`).
- **MEDIUM:** 5 API call sites receive user-influenced text without system prompts.
- **Structural gap:** `CallSiteConfig` has no identity/constraint fields — all constraints
  are ad-hoc in caller code. Centralized constraint management recommended for V4.

Full audit details in `docs/audits/2026-03-18-call-site-identity-audit.md` (if created)
or in session transcript.

---

## Plan Required (Design Before Building)

### P-1: Self-Healing Server
**Source:** Video 2 — OpenClaw "Self-Healing Home Server" pattern
**What:** Genesis already has health monitoring (neural monitor), Phase 9
autonomy, and Phase 6 procedure learning. What's missing: specific remediation
procedures wired to detected failures.
Examples:
- Qdrant down → restart systemd service
- Disk filling → identify and rotate old logs
- Process crash → check logs, restart with exponential backoff
- AZ web UI unresponsive → restart run_ui.py
- Ollama sibling container unreachable → alert (can't fix, but can degrade gracefully)
**Needs:** Design doc mapping health signals → remediation procedures → governance
level (which actions are L2-auto vs L3-confirm vs L4-alert-only).
**Scope:** V3 (infrastructure exists)
**Priority:** High — this is table-stakes for an "autonomous agent"

### P-2: Bugs-First Enforcement for Genesis Development
**Source:** Video 2 — OpenClaw "Autonomous Game Dev Pipeline" with bugs-first
**What:** Systematic enforcement that known bugs are fixed before new feature
work begins. Currently we have TDD + ruff + pytest, but no gate that says
"there are 3 known bugs — fix those first."
Possible implementation:
- Bug backlog tracked in GitHub issues (labeled)
- Pre-plan-execution check: "Are there open bug issues? Fix those first."
- CC dispatch priority: bug-fix sessions before feature sessions
- Awareness loop signal: open bug count as a health metric
**Needs:** Design doc defining the bug tracking → enforcement → governance flow.
**Scope:** V3 (process + tooling)
**Priority:** High — we should be eating our own dogfood

### P-3: AgentMail Integration
**Source:** Video 3 — "Why Every Claude Code User Needs To Try AgentMail"
**What:** Dedicated Genesis email identity via AgentMail API. Genesis runs CC
24/7 (background sessions, foreground sessions, guardian CC planned). AgentMail
gives Genesis:
- Unique email address for outbound communication
- Two-way threaded conversations
- Draft/approval workflow (maps to L3/L4 governance)
- Inbound email processing (supplement existing inbox evaluation)
**Integration point:** Both the CC skill (Genesis runs CC 24/7 — background
sessions, foreground, guardian CC planned) AND direct API calls from awareness
loop + surplus system.
**Needs:** Spike: sign up, get API key, test send/receive from Python.
**Scope:** V3 (direct API integration)
**Priority:** Medium

### P-4: AI Agent Development as a Service
**Source:** Video 1 — "Selling AI Agents To Make Money"
**What:** Genesis as a force multiplier for a one-person AI agency. User sells
custom agent development ($30-150K/client), Genesis does the heavy lifting:
- Research client requirements
- Design agent architecture
- Scaffold the project
- Implement, test, deploy
- Learn from each project → better at future projects
**Needs:** Further discussion on feasibility, scope, and whether this is a
direction the user actually wants to pursue.
**Scope:** V4 (requires more mature autonomy)
**Priority:** Low — theoretical, discuss before planning

### P-5: Internal Codebase Recon Pipeline (Bugs-First Companion)
**Source:** Video 2 discussion — turning the SEER/surplus research pipeline inward
**What:** Use the same tiered pipeline architecture we built for external
research (SEER/Recon MCP + surplus compute), but pointed at Genesis's own
codebase. Proactive, continuous code quality engine:

**Pipeline tiers:**
1. **Free/surplus compute (broad scan):** Cheap models or pattern-based tooling
   scour the codebase for: potential bugs, dead code, missing test coverage,
   architecture violations, stale TODOs, GROUNDWORK tags without progress,
   documentation drift, unchecked error paths, security issues, performance
   concerns. Cast a wide net — quantity over precision at this tier.
2. **Intermediate models (filter + consolidate):** Stronger but still cheap
   models (Haiku, groq, mistral) filter signal from noise. "Is this actually
   a bug or a false positive?" Err on the side of more information — let
   through anything plausible. Consolidate duplicates, group by subsystem.
3. **Opus (final judgment):** Gets the consolidated findings, investigates the
   actual code as needed, and makes the call: real issue → GitHub issue (feeds
   P-2 bugs-first enforcement), false positive → discard, improvement
   opportunity → queue for surplus.

**Key properties:**
- Proactive — runs during surplus compute, not on-demand
- Complements P-2 (bugs-first enforcement) by feeding the bug backlog
- Same architectural pattern as external research pipeline, different target
- Both external and internal recon are proactive surplus activities

**Needs:** Design doc mapping: scan targets, tier boundaries, governance for
auto-filing issues vs requiring user review.
**Scope:** V3 (pipeline infrastructure exists, just needs an inward-facing mode)
**Priority:** High — direct companion to P-2

### P-6: Dashboard Visibility for Modules & Pipeline
**Source:** Video 2 discussion — need visibility into what Genesis is doing
**What:** The web UI needs to surface:
- **Capability modules** — what modules exist, their status, what they do
- **SearXNG** — SearXNG instance status, query history, availability
- **Research/recon pipeline** — when it's running, what it's scanning (external
  vs internal), what it's found, current tier status
- **Surplus compute usage** — what surplus is being spent on, history of surplus
  activities, queue depth, what's pending
- **Pipeline provenance** — for any finding/result, show the chain: which scan
  found it → which intermediate model filtered it → what Opus decided

This is a significant dashboard effort. Ties into: neural monitor (health),
surplus system (activity), capability modules (inventory), and the recon
pipeline (results). Will require substantial design thinking.

**Needs:** Design doc for dashboard information architecture. What panels,
what data sources, what refresh rates.
**Scope:** V3 (existing AZ UI overlay pattern)
**Priority:** Medium-High — observability into what Genesis is doing is
non-negotiable for trust. "If you can't see it, you can't trust it."

### P-7: Mobile App Development via Genesis
**Source:** Video 4 — Xcode + Claude/Codex for mobile apps
**What:** Not about Genesis architecture — about Genesis building mobile apps
FOR the user. The video validates: "vibe coding tools for prototyping UX,
Xcode + AI agent for real builds."

Genesis could run this pipeline:
- User describes app idea / provides prototype (from vibe coding tool)
- Genesis researches: market, App Store requirements, technical approach
- CC sessions generate the Xcode project, Swift code, UI, tests
- Genesis iterates on the build (test → fix → test)
- User reviews, submits to App Store

**Open questions:**
- Xcode requires macOS. Genesis runs on Ubuntu. Options:
  - Remote Mac (Mac Mini, MacStadium, AWS Mac instances)
  - Cross-platform framework (Flutter, React Native) that builds on Linux
    with final compilation on a CI Mac
  - User's local Mac as the Xcode host, Genesis pushes code to it
- CC can work with Swift/Xcode projects directly (per video)
- This is another "Genesis as productivity multiplier" use case, like P-4

**Needs:** Spike on: can CC dispatch build Swift/Xcode projects remotely?
What's the minimal Mac infrastructure needed? Or is a cross-platform
framework the pragmatic path?
**Scope:** V4 (depends on Genesis working on external projects)
**Priority:** Medium — concrete revenue opportunity, user has expressed
interest before

---

## Future Consideration (Not Now)

### F-1: Voice Channel (Phone Calls)
**Source:** Video 2 — OpenClaw voice/phone capabilities
**What:** Voice channel for Genesis via Vapi/Telnyx. STT→bridge→TTS.
Proactive callbacks (Genesis calls user on alerts).
**Cost:** ~$30-80/month for moderate use
**Why later:** Requires subscription, telephony provider setup. Architecture is
compatible (voice is just another channel). Not blocking anything.
**Scope:** V4 (channel framework)

### F-2: Channel Framework Generalization
**Source:** Cross-cutting theme from all videos
**What:** Abstract channel providers (Telegram, web, voice, email, SMS) behind
a common interface. OpenClaw has 8+ channels; we have 2.
**Scope:** V4

### F-3: Proactive Callbacks
**Source:** Video 2 — OpenClaw proactive callback feature
**What:** Genesis calls the user (voice) or texts (SMS) for high-priority
alerts instead of waiting for the user to check Telegram.
**Scope:** V4 (depends on F-1)

### F-4: Autonomous Value Metrics
**Source:** Video 1 — "narrow + measurable" success pattern
**What:** Genesis tracks and reports: hours of real work done autonomously per
week, accuracy of autonomous actions, tasks completed without user intervention.
**Scope:** V4

### F-5: MCP Event-Driven Integration
**Source:** MCP Roadmap 2026 — "on the horizon" triggers/events
**What:** When MCP spec adds event-driven updates / triggers, Genesis's
awareness loop could subscribe to MCP server events instead of polling.
More reactive architecture. Also: `.well-known` discovery for auto-detecting
MCP servers. Monitor spec progress at https://modelcontextprotocol.io/development/roadmap
**Scope:** V4 (dependent on spec stabilization, tentatively June 2026)

### F-6: Scrapr Technique (API Interception Pattern)
**Source:** Scrapr Product Hunt listing
**What:** The product itself is too immature (MVP, no docs), but the technique
is valuable: instead of scraping HTML/DOM, intercept the site's own API calls
and extract structured data from those. "Extract from the data source, not
the presentation layer." When Genesis needs reliable scraping of a specific
site, analyze its network requests first.
**Scope:** Never (as a dependency), technique noted for reference

### F-7: Agentic Retrieval Loop (System-Wide, Not Just Reflection)
**Source:** NVIDIA NeMo Retriever — agentic retrieval pipeline (#1 ViDoRe v3)
**What:** Iterative agent-retriever loop: think → retrieve → evaluate → reformulate
→ retrieve again. NVIDIA shows 5-8 point NDCG improvement over single-pass.

Apply broadly, not just deep reflection — anywhere latency tolerates it:
- **Latency-sensitive paths** (conversation, micro reflection): keep single-pass
- **Background tasks** (surplus, recon, deep reflection, strategic): use iterative
- **Procedure recall**: reformulate if first query misses ("content fetching" →
  try "web scraping" → try "YouTube" → try "video processing")
- **Memory recall**: iterative refinement when first pass doesn't answer the question
- **Recon pipeline (P-5)**: this IS the agentic retrieval pattern applied to code

Agent quality matters more than embedding quality — Opus closes gap between weak
and strong embeddings. Our qwen3-0.6b could be compensated by strong reasoning.
RRF fallback when agent hits step limit (already our approach).

**Note:** NOT abandoning MCP. NVIDIA's critique was about MCP-as-separate-process
overhead. Our FastMCP is already in-process. We stay MCP-native.
**Scope:** V3/V4 (background tasks now, latency-sensitive paths later)

### F-8: Bayesian Behavioral Reinforcement (Broader Than User Model)
**Source:** Google Bayesian Teaching for LLMs (InfoQ) + user discussion
**What:** Use Bayesian methods not just for user model priors, but as a general
mechanism for making Genesis reliably follow its own instructions. The core
problem: CLAUDE.md, identity files, steering rules, and procedures exist but
the LLM drifts from them under task pressure. Hooks are "blunt instruments"
and can be overridden. We need something between "advisory text the LLM ignores"
and "hard hook that blocks."

Bayesian approach could provide:
1. **Instruction adherence tracking** — measure how often each CLAUDE.md rule,
   STEERING.md directive, and procedure is actually followed vs ignored. Maintain
   probability distributions over compliance rates per rule.
2. **Reinforcement through evidence** — when a rule is followed and the outcome
   is good, increase the weight. When ignored and the outcome is bad, surface
   the rule more aggressively (promote to higher activation tier, inject into
   more contexts).
3. **User model updating** — maintain explicit probability distributions over
   user preferences instead of LLM-driven summarization. Inject updated
   distributions into prompts rather than relying on in-context learning.
4. **Principled confidence** — "When you can compute something exactly (Bayesian
   update), don't ask the LLM to approximate it." Validates our Laplace-smoothed
   procedure confidence. Extend to instruction compliance scoring.

This is the bridge between "knowledge exists" and "knowledge reliably influences
behavior" that the Procedure Activation Architecture addresses mechanically
(hooks, injection). Bayesian reinforcement addresses it statistically — making
the LLM's own in-context behavior more reliable over time.

**Needs:** Design doc mapping: which instructions to track, how to measure
compliance, how to inject reinforcement signal, how this interacts with the
procedure activation system.
**Scope:** V4 (requires instrumentation of instruction following)

### F-9: /loop for Continuous Codebase Monitoring
**Source:** CC 2.0 features + user discussion
**What:** Use CC's `/loop` command for continuous non-destructive monitoring
and deep-diving through the codebase. NOT for building (too risky), but for:
- Infrastructure review (continuous audit of code health)
- Codebase deep-dive (systematic exploration until complete)
- Security monitoring (watch for new vulnerabilities)
- Ties into the infrastructure review call site being designed in parallel
The `/loop` pattern is: run a read-only analysis on a schedule until a
condition is met or the session ends. Safe because it only reads, never writes.
**Scope:** V3/V4 (investigate integration with infrastructure review)

### F-10: Antigravity / ADK Integration
**Source:** Google ADK video — vibe coding with Antigravity
**What:** People already use CC with Antigravity. How can Genesis leverage this?
- ADK callback pattern (before/after model) is generalizable
- Antigravity's vibe coding approach for rapid prototyping
- Model Armor for content sanitization at ingestion points
- Integration path: can Genesis dispatch Antigravity sessions the way it
  dispatches CC sessions? What does the interface look like?
**Needs:** Investigation into CC + Antigravity interop, ADK Python SDK
compatibility with our stack.
**Scope:** V4 (investigate integration path)

### F-11: Model-Level Pre/Post Callbacks in Router
**Source:** Google ADK + Antigravity video — before-model/after-model callbacks
**What:** Add interception points to Genesis's LLM router:
- **Before-model callback**: sanitize input, check for injection, enforce context limits
- **After-model callback**: validate output, check for hallucination markers, enforce response constraints
Currently Genesis intercepts at the tool boundary (PreToolUse hooks) but not
at the model boundary. Google's ADK pattern closes this gap. Every LLM call
through the router would have configurable pre/post hooks.
**Scope:** V4 (requires router refactoring)

### F-12: Quick Security Wins from OpenShell (Not Enterprise Adoption)
**Source:** NVIDIA NemoClaw / The Register article
**What:** OpenShell is enterprise-oriented and Genesis isn't. But any quick,
easy security wins are welcome. Genesis's design should dwarf both consumer
and enterprise OpenClaw equivalents — not by adopting their enterprise patterns,
but by being architecturally superior. Cherry-pick specific techniques:
- Policy-based security rules (declarative, if open-sourced)
- Network guardrail patterns (which outbound connections to allow/block)
- Filesystem isolation techniques
Don't adopt the framework wholesale. Extract what's useful.
**Scope:** V4 (cherry-pick, not adopt)

### F-13: Browser-Use Self-Improving Agents
**Source:** Tier 2 evaluation — browser-use patterns
**What:** Investigate browser-use frameworks where agents self-improve by
observing their own browser interactions — learning navigation patterns,
correcting failed actions, building procedural memory for web tasks. Relevant
to Genesis's existing Playwright integration and procedure learning system.
**Scope:** Tier 3 investigation — evaluate when browser-based autonomy matures

### F-14: Skill System Organization (Context/Execution/Meta)
**Source:** Simon Scrapes "Agentic Operating System" video
**What:** Categorize skills explicitly as:
- **Context skills** — build/maintain shared state (brand context, user model)
- **Execution skills** — do actual work (research, write, deploy)
- **Meta skills** — manage the system itself (learning, feedback, skill refinement)
This organizational pattern makes skill interconnection explicit. Genesis
already has all three types but doesn't categorize them as such.
**Scope:** V3 (organizational, update skill conventions doc)

---

## Completed

### Procedure Activation Architecture
**Source:** Evaluation session discussion on YouTube failure + learning gaps
**Completed:** 2026-03-18
**What:** Full 4-layer procedure activation system: PreToolUse advisor hook,
SessionStart injection, skill-embedded procedures, CLAUDE.md rules. 7 seed
procedures, automatic ingestion from triage pipeline, hourly promotion.
8 commits, ~1200 lines, 2505 tests passing.

### QW-7: Prompt Injection Defense
**Source:** Google ADK + Antigravity video — Model Armor callbacks
**Completed:** 2026-03-18
**What:** Three-layer defense for third-party content injection:
1. ContentSanitizer with XML boundary markers at all ingestion points
   (inbox, web fetch, web search)
2. Pattern detection (8 patterns, log-only, never blocks) with risk scoring
3. PostToolUse hook injecting real-time injection warning after WebFetch
   and all Playwright browser tools — fires in ALL sessions
Plus: inbox CC sessions restricted via disallowedTools, INBOX_EVALUATE.md
updated with injection awareness section.
73 new tests (49 sanitizer + 24 hook).

### P-1: Self-Healing Server
**Source:** Video 2 — OpenClaw "Self-Healing Home Server" pattern
**Completed:** 2026-03-18
**What:** RemediationRegistry mapping health probes to corrective actions with
governance levels (L2 auto, L3 confirm, L4 alert). 5 default remediations
(Qdrant restart, /tmp cleanup, awareness restart, disk alert, Ollama alert).
Fixes: Qdrant mid-operation crash fallback in store.py, watchdog NOTIFY →
outreach BLOCKER, stabilization cooldown preventing reset oscillation.
New probes: /tmp usage, disk usage.
37 new tests. Design doc: docs/architecture/genesis-v3-self-healing-design.md.

### QW-1-4-8-9: Tier 1 Quick Wins
**Completed:** 2026-03-18
**What:** Security TDD (272 tests), skill conventions overhaul, idempotent
outreach dedup, Cloudflare /crawl provider, identity audit findings.

### P-5: Internal Codebase Recon Pipeline
**Completed:** 2026-03-18
**What:** Inward-facing recon pipeline using the same tiered architecture as
external research. Proactive code quality scanning during surplus compute,
feeding the bugs-first enforcement backlog (P-2).

### P-6: Dashboard Visibility for Modules & Pipeline
**Completed:** 2026-03-18
**What:** Dashboard panels surfacing capability modules, SearXNG status,
research/recon pipeline activity, surplus compute usage, and pipeline
provenance. Built on the existing AZ UI overlay pattern.

### P-2: Bugs-First Enforcement (Partial)
**Note:** GitHub issue integration portion deferred to V4 — Genesis is a
private single-developer repo; GitHub API in the awareness loop is premature.
See `docs/plans/v4-research-driven-features-spec.md` §14.

(Other completed items move here when done)
