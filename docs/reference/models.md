<!-- This file is the model catalog — what's available and what each model is good at.
     For which model is assigned to which call site, see the Model Routing Registry:
     docs/architecture/genesis-v3-model-routing-registry.md
     Read by the task decomposer and routing registry. Reviewed weekly by the dream cycle. -->

# Model Pool

## THE HEAVY LIFTERS

Specialized for reasoning, systems architecture, and deep technical logic.

### GLM-5 (Thinking)

- **API ID:** `z-ai/glm-5` (OpenRouter) / `glm-5` (Z.AI direct)
- **Role:** Complex Planning & Systems Engineering
- **Context:** 200k tokens
- **Intelligence Tier:** S (Chain-of-Thought reasoning)
- **Cost:** $0.80/$2.56 per MTok (input/output via OpenRouter) — ~$1.24 blended via DeepInfra
- **Free Tier:** Z.AI/BigModel — 20M free tokens for new users (api.z.ai); also on Nvidia NIM (5,000 credits, 40 RPM)

**Best At:**
1. Multi-step Architecture: Planning microservices and system diagrams.
2. Root Cause Analysis: Tracing intermittent bugs across distributed systems.
3. Technical Planning: Deep-thinking logic at competitive pricing.

**Worst For:**
- Response Speed: High latency due to internal "thinking" cycles.
- Creative Tone: Output is often dry, academic, and purely utilitarian.
- Massive Repos: 200k limit is tight for ingesting multi-gigabyte codebases.

### DeepSeek V4

- **API ID:** `deepseek/deepseek-v4` (OpenRouter)
- **Role:** Coding & Codebase Building
- **Context:** 10M tokens
- **Intelligence Tier:** S (Logic Efficiency)
- **Cost:** ~$0.30/$1.20 per MTok (input/output)
- **Free Tier:** DeepSeek V3.2 available on Nvidia NIM (5,000 credits, 40 RPM)

**Best At:**
1. Repository Scaffolding: Generating entire app structures (Next.js/Rust) in one go.
2. Math & Logic: Top-tier algorithmic density for a low price.
3. Multi-file Reasoning: Handling logic that spans dozens of files simultaneously.

**Worst For:**
- Natural Prose: Writing can feel slightly robotic or "translated."
- Formatting Nuance: Occasionally ignores specific "soft" styling instructions.
- Censorship: Heavily filtered on sensitive political or cultural topics.

### GPT-5.3 Codex

- **API ID:** `openai/gpt-5.3-codex` (OpenRouter)
- **Role:** Agentic Code Debugging
- **Context:** 400k tokens
- **Intelligence Tier:** S (Autonomous Agentic)
- **Cost:** $1.75/$14.00 per MTok (input/output) — ~$7.80 blended. Expensive.
- **Free Tier:** None

**Best At:**
1. Terminal Autonomy: Can independently run and fix code in a live terminal.
2. Zero-Shot Accuracy: Highest "first-try" success rate for fixing complex bugs.
3. Technical Recall: Zero degradation of memory across its 400k window.

**Worst For:**
- Budget Work: Expensive for routine scripting.
- Creative Brainstorming: Extremely literal; lacks the "creative spark" of Claude.
- Multilingual Coding: Heavily optimized for English-language documentation.

### Claude 4.6 Opus

- **API ID:** `anthropic/claude-opus-4-6` (OpenRouter)
- **Role:** Weekly Comprehensive Review
- **Context:** 1M tokens
- **Intelligence Tier:** S (Philosophical/Moral reasoning)
- **Cost:** $5.00/$25.00 per MTok (input/output)
- **Free Tier:** None

**Best At:**
1. Strategic Synthesis: Summarizing 50+ mixed documents into a high-level strategy.
2. Moral/Creative Nuance: Catching subtle "vibe" or ethical issues in team comms.
3. Trustworthiness: Lowest rate of "hallucinated" logic on the market.

**Worst For:**
- Speed: The slowest frontier model available; agonizes over its output.
- Operational Cost: Not for high-volume automation.
- Refusal Rates: Highly sensitive safety filters can trigger on benign requests.

---

## THE ALL-ROUNDERS

The daily drivers for professional productivity and general intelligence.

### Claude 4.5 Sonnet

- **API ID:** `anthropic/claude-sonnet-4-5-20250929` (OpenRouter)
- **Role:** Good overall model
- **Context:** 1M tokens
- **Intelligence Tier:** A (Professional Utility)
- **Cost:** $3.00/$15.00 per MTok (input/output) — long context >200k: $6.00/$22.50
- **Free Tier:** None

**Best At:**
1. Professional Writing: Best "corporate-safe" tone out of the box.
2. Visual Reasoning: Exceptional at reading complex charts and UX screenshots.
3. Consistency: Very low variance in quality between different API calls.

**Worst For:**
- Pure Logic Puzzles: Can struggle with the "trick" math that Opus handles.
- Speed: Slower than "Flash" models for simple, repetitive chat tasks.
- Risk Aversion: Often refuses tasks that require "playing devil's advocate."

### MiniMax M2.5

- **API ID:** `minimax/minimax-m2.5` (OpenRouter)
- **Role:** Small Context Generalist
- **Context:** 205k tokens
- **Intelligence Tier:** A (Office Logic)
- **Cost:** $0.30/$1.20 per MTok (Standard) or $0.30/$2.40 (Lightning, 2x speed)
- **Free Tier:** None

**Best At:**
1. Office Deliverables: Perfect output for Word, PPT, and Excel financial models.
2. Roleplay: Surprisingly high EQ and adaptability to specific personas.
3. Value: Very cheap for its capability level.

**Worst For:**
- Obscure Facts: High hallucination rate on niche historical or legal details.
- Coding Security: Often generates working code that contains security vulnerabilities.
- Conversation Length: Tends to lose focus after 20+ turns of dialogue.

### Kimi K2.5

- **API ID:** `moonshotai/kimi-k2.5` (OpenRouter)
- **Role:** Agent Swarms & Project Management
- **Context:** 2M tokens
- **Intelligence Tier:** A (S if agentic use case) (Multimodal Agentic)
- **Cost:** $0.60/$3.00 per MTok (Moonshot direct) — $0.45/$2.25 via DeepInfra
- **Free Tier:** Available on Nvidia NIM (5,000 credits, 40 RPM)

**Best At:**
1. Parallel Research: Spawning sub-agents to research multiple topics at once.
2. Multi-File Handling: Native support for handling large .zip or .tar uploads.
3. Long-Context Summarization: Synthesizing massive amounts of raw research.

**Worst For:**
- Single-Thread Speed: Slower than Gemini Flash for simple, direct Q&A.
- Mathematical Precision: Weaker than DeepSeek on pure arithmetic calculation.
- Reliability: Beta "swarm" features can occasionally crash or loop.

### Mistral Large 3

- **API ID:** `mistralai/mistral-large-latest` (OpenRouter)
- **Role:** Low Hallucination / High Reliability
- **Context:** 128k tokens
- **Intelligence Tier:** A (Enterprise Compliance)
- **Cost:** $2.00/$6.00 per MTok (input/output)
- **Free Tier:** Mistral free tier — ALL models, 2 RPM, 1B tokens/month (see Free Tier Terms)

**Best At:**
1. JSON Instruction: Follows strict formatting and data schemas perfectly.
2. Multilingual Mastery: Superior nuance in French, German, and Spanish.
3. Data Privacy: The gold standard for secure, on-prem enterprise setups. Free tier data NOT used for training.

**Worst For:**
- Creative Flourish: Output is often "boring," dry, and overly utilitarian.
- Context Size: 128k is now considered small compared to the 1M+ standard.
- Narrative Flow: Struggles with long-form storytelling or creative prose.

---

## THE SPECIALISTS

Optimized for specific tasks: speed, video reasoning, and massive memory ingestion.

### Gemini 3 Pro

- **API ID:** `google/gemini-3-pro` (OpenRouter) / `gemini-3-pro-preview` (Google AI)
- **Role:** Multimodal Reasoning Agent
- **Context:** 2M tokens
- **Intelligence Tier:** S (Multimodal Reasoning)
- **Cost:** $2.00/$12.00 per MTok (≤200k) — $4.00/$18.00 (>200k)
- **Free Tier:** Gemini API free tier — see Free Tier Terms below

**Best At:**
1. Video/Audio Intel: "Watching" a 1-hour meeting and identifying key moments.
2. Native Integration: Seamlessly reasoning across images and text simultaneously.
3. Live Search: Best-in-class integration with real-time Google search data.

**Worst For:**
- Text-Only Price: Too expensive (~$7 blended) if you aren't using the multimodal features.
- Verbosity: Has a habit of being overly wordy and "preachy" in its advice.
- Sycophancy: Persistently agreeable even when explicitly told not to be. Will validate
  weak reasoning rather than challenge it. Do not use for critical review or adversarial
  analysis — it will confirm your biases instead of exposing them.
- Code Logic: Can be inconsistent with complex software architecture.

### Gemini 3 Flash

- **API ID:** `google/gemini-3-flash` (OpenRouter) / `gemini-3-flash-preview` (Google AI)
- **Role:** Codebase Ingestion & Speed
- **Context:** 1M tokens
- **Intelligence Tier:** B/A (A if thinking mode)
- **Cost:** $0.50/$3.00 per MTok (input/output)
- **Free Tier:** Gemini API free tier — see Free Tier Terms below

**Best At:**
1. Speed: Nearly instant "first token" response even with huge context.
2. Bulk Summarization: Cleaning up and indexing 100k+ lines of code for pennies.
3. Extraction: Pulling specific data points out of massive unorganized logs.

**Worst For:**
- Complex Logic: Fails at multi-stage math or "System 2" thinking puzzles.
- Emotional Intelligence: Misses subtle sarcasm or subtext in human chat.
- Sycophancy: Same as Gemini 3 Pro — validates premises instead of challenging them.
  Observed even with explicit counter-instructions. Factor this into any task requiring
  critical analysis or design review.
- Software Design: Great at reading code, but bad at writing it from scratch.

### Llama 4 Scout

- **API ID:** `meta-llama/llama-4-scout` (OpenRouter)
- **Role:** Infinite Memory / Library Ingestion
- **Context:** 10M tokens
- **Intelligence Tier:** B (Memory Optimized)
- **Cost:** $0.18/$0.63 per MTok (OpenRouter) — as low as $0.11 blended via Groq
- **Free Tier:** Free on OpenRouter (free tier variant); Nvidia NIM (5,000 credits, 40 RPM)

**Best At:**
1. Library Ingestion: Loading entire software documentation sets in one pass.
2. Deep Recall: Finding a needle in a haystack within 5,000+ pages of text.
3. Local Deployment: High performance-per-parameter for self-hosted setups.

**Worst For:**
- Middle-Context Accuracy: Precision can dip slightly in the 5M-8M token range.
- Reasoning Density: Not as "smart" as Opus or GPT-5 for creative strategy.
- Conversational Flow: Can feel verbose and repetitive in casual chat.

### Claude 4.5 Haiku

- **API ID:** `anthropic/claude-haiku-4-5-20251001` (OpenRouter)
- **Role:** "Human Sounding" Small Model
- **Context:** 200k tokens
- **Intelligence Tier:** B (Empathy & Speed)
- **Cost:** $1.00/$5.00 per MTok (input/output)
- **Free Tier:** None

**Best At:**
1. Conversational Tone: Warm, empathetic, and indistinguishable from a human.
2. Formatting Cleanup: Turning messy raw text into beautiful Markdown.
3. Cost/Speed: Perfect for high-traffic customer support or basic chat bots.

**Worst For:**
- Hard Sciences: Fails at complex physics, chemistry, or math proofs.
- Factuality: Higher hallucination rate than Sonnet or Opus on obscure facts.
- Large-Scale Systems: Struggles to design full backend architectures.

### Grok 4

- **API ID:** `xai/grok-4` (xAI direct)
- **Role:** Adversarial Analysis & Devil's Advocate
- **Context:** 256k tokens
- **Intelligence Tier:** S (Unfiltered Reasoning)
- **Cost:** ~$3.00/$15.00 per MTok (estimated)
- **Free Tier:** None

**Best At:**
1. Contrarian Analysis: Willing to argue the unpopular position convincingly.
2. Unfiltered Output: Fewer safety refusals than Claude or GPT on legitimate tasks.
3. Technical Debate: Strong at finding flaws in reasoning and design.

**Worst For:**
- Consistency: Output quality variance is higher than Claude or GPT.
- Structured Output: Less reliable at following strict JSON schemas.
- Enterprise Compliance: Not suitable for regulated environments.

### Grok 4.1 Fast

- **API ID:** `xai/grok-4.1-fast` (xAI direct)
- **Role:** Speed-Optimized Adversarial
- **Context:** 256k tokens
- **Intelligence Tier:** A (Fast Reasoning)
- **Cost:** ~$1.00/$5.00 per MTok (estimated)
- **Free Tier:** None

**Best At:**
1. Quick Counterarguments: Faster alternative to Grok 4 for simpler reviews.
2. Bulk Analysis: When you need adversarial review at higher throughput.

**Worst For:**
- Deep Reasoning: Trades depth for speed compared to Grok 4.
- Same consistency issues as Grok 4.

### GPT-5.4

- **API ID:** `openai/gpt-5.4` (OpenRouter)
- **Role:** Long-Context Agentic Work & Computer Use
- **Context:** 1M tokens
- **Intelligence Tier:** S (Agentic Reasoning)
- **Cost:** ~$2.50/$12.00 per MTok (input/output, estimated)
- **Free Tier:** None

**Best At:**
1. Computer Use: First mainline model with built-in computer-use capabilities (build-run-verify-fix loop).
2. Compaction Training: Purpose-built for context compression during long agent trajectories — preserves key info while reducing token count.
3. Tool-Heavy Workloads: Measurably better token efficiency on multi-step tool calling vs predecessors.
4. Long-Context Agent Trajectories: 1M context + compaction = can run extended autonomous sessions without degradation.
5. Factuality: 33% fewer false claims than GPT-5.2 (measured on user-flagged error prompts).
6. Agentic Web Search: Multi-source synthesis, especially for hard-to-locate information.

**Worst For:**
- Creative Writing: Less distinctive voice than Claude.
- Cost: Expensive for bulk background work.
- Interactive Thinking: Plan-alteration UX requires human-in-loop, irrelevant for autonomous use.

**Genesis Relevance:**
- Strong candidate for **computer-use tasks** that Genesis dispatches (browser automation, desktop interaction).
- **Co-orchestrator potential**: For tasks requiring extended agentic trajectories (multi-hour research, complex multi-step execution), GPT-5.4's compaction training could outperform Claude on token efficiency.
- **Disagreement gate partner**: Different training data and reasoning patterns from Claude. Useful for V4 disagreement-based verification (two models must agree on high-stakes decisions).
- Route via OpenRouter alongside existing model pool.

### GPT-5 Nano

- **API ID:** `openai/gpt-5-nano` (OpenRouter)
- **Role:** Ultra-Cheap Paid Fallback
- **Context:** 128k tokens
- **Intelligence Tier:** B (Budget Reasoning)
- **Cost:** ~$0.05/$0.20 per MTok (input/output)
- **Free Tier:** None

**Best At:**
1. Cost: Cheapest viable paid fallback for background extraction work.
2. Speed: Very fast inference.
3. Structured Output: Reliable at simple schema-following tasks.

**Worst For:**
- Complex Reasoning: Not suitable for judgment calls.
- Nuance: Misses subtlety in analysis tasks.

### GPT-5 Mini

- **API ID:** `openai/gpt-5-mini` (OpenRouter)
- **Role:** Mid-Tier Paid Fallback
- **Context:** 256k tokens
- **Intelligence Tier:** B+ (Capable Budget)
- **Cost:** ~$0.15/$0.60 per MTok (input/output)
- **Free Tier:** None

**Best At:**
1. Value: Strong capability-to-cost ratio for moderate tasks.
2. Larger context than Nano for tasks needing more input.

**Worst For:**
- Same limitations as Nano, slightly less severe.

### Qwen 3.5 Plus

- **API ID:** `qwen/qwen3.5-plus` (Alibaba Cloud)
- **Role:** Cost-Effective Judgment & Agent Tasks
- **Context:** 128k tokens
- **Intelligence Tier:** A (Agent Optimized)
- **Cost:** $0.40/$2.40 per MTok (input/output)
- **Free Tier:** None

**Best At:**
1. Agent Benchmarks: Top scores on agentic task completion.
2. Value: Strong reasoning at fraction of Sonnet/Opus cost.
3. Structured Output: Reliable JSON and schema compliance.

**Worst For:**
- English Nuance: Occasional awkward phrasing in natural language.
- Creative Tasks: Functional but uninspired output.

### Qwen3-Max-Thinking

- **API ID:** `qwen/qwen3-max-thinking` (Alibaba Cloud)
- **Role:** Deep Reasoning Alternative
- **Context:** 128k tokens
- **Intelligence Tier:** S (Chain-of-Thought)
- **Cost:** $1.20/$6.00 per MTok (input/output)
- **Free Tier:** None

**Best At:**
1. Mathematical Reasoning: Strong chain-of-thought on complex problems.
2. Multi-Step Planning: Good at decomposing complex tasks.

**Worst For:**
- Speed: Thinking mode adds latency.
- Cost: Expensive for its tier if not using reasoning capabilities.

---

## Selection Cheat Sheet

Loose guidance — not prescriptive. Use your judgment based on the task requirements.

- **The Architect:** GLM-5 / Opus
- **The Programmer:** DeepSeek V4 / Codex
- **The Researcher:** Gemini 3 Flash / Llama 4 Scout

---

## Free Tier Terms

### Gemini API (Google AI Studio)
- **Endpoint:** `generativelanguage.googleapis.com` (NOT Vertex AI)
- **Setup:** Get API key from ai.google.dev — no payment required
- **Rate limits** (as of Feb 2026, may change without notice):
  - Gemini 2.5 Flash: 10 RPM, 250 RPD, 250k TPM
  - Gemini 2.5 Pro: 5 RPM, 100 RPD, 250k TPM
  - Gemini 3 Flash: 1500 RPD, 15 RPM (used by dream cycle with thinking enabled)
  - Gemini 3 Pro: check ai.google.dev/gemini-api/docs/rate-limits for current limits
- **IMPORTANT:** Free tier data MAY be used for model training
  - Paid tier (Tier 1+, requires Cloud Billing) guarantees data is NOT used for training
  - If sending proprietary/sensitive data, use paid tier
- RPD resets at midnight Pacific Time
- EU/EEA/UK/Switzerland restricted on free tier
- Full 1M token context window available on free tier
- Free tier limits can change without warning (Google cut limits 50-80% in Dec 2025)

### Nvidia NIM
- **Endpoint:** build.nvidia.com
- **Setup:** Create Nvidia developer account — no payment required
- **Rate limits:** 40 RPM, ~5,000 total API credits (NOT unlimited despite marketing)
- No daily cap, but credit-capped (credits do not refresh)
- **Available models:** Kimi K2.5, Llama 4 Scout, DeepSeek V3.2, GLM-5
- Best for testing and prototyping only. Not production-ready.
- Once credits exhausted, must pay or create new account

### Z.AI / BigModel (GLM-5)
- **Endpoint:** api.z.ai (international) / open.bigmodel.cn (China)
- **Setup:** Register at z.ai — no payment required for free credits
- **Free allocation:** 20 million tokens for new users
- After free credits: pay-as-you-go at $0.80/$2.56 per MTok
- Also available: Puter.js integration (free, no API key, no usage restrictions)
- Note: GLM-5 may not yet be on OpenRouter — use z.ai API directly

### Mistral Free Tier
- **Endpoint:** `api.mistral.ai`
- **Setup:** Create account at console.mistral.ai — no payment required
- **Access:** ALL Mistral models including Mistral Large 3 (strongest)
- **Rate limits:** 2 RPM, 1B tokens/month
- **Privacy:** Data NOT used for model training (unlike Gemini free tier)
- 2 RPM is sufficient for scheduled background tasks that fire sequentially
- Genesis's primary free compute source for Bucket 2 background work

### Groq Free Tier
- **Endpoint:** `api.groq.com`
- **Setup:** Create account at console.groq.com — no payment required
- **Best model:** Llama 3.3 70B Versatile
- **Rate limits:** 30 RPM, 1,000 RPD, ~6,000 tokens/min
- Best for burst scenarios or when Mistral's 2 RPM limit is too slow

### OpenRouter Free Tier
- ~29 models available as free variants on OpenRouter
- **Rate limits:** 20 RPM, 200 RPD (shared across all free models)
- Includes Llama 4 Scout, various community models
- Use as overflow when other free sources are exhausted

---

## Effort Level Assignments

### Current State (as of 2026-04-12)

Effort levels are set **per-invocation** in code, not per-call-site in routing config.

| Dispatch Context | Model | Effort | Set In |
|-----------------|-------|--------|--------|
| Light reflection | Haiku | LOW | `reflection_bridge._effort_for_context()` |
| Deep reflection | Sonnet | HIGH | `reflection_bridge._effort_for_context()` |
| Strategic reflection | Opus | MAX | `reflection_bridge._effort_for_context()` |
| Task execution | Sonnet | MEDIUM | `session_config.build_task_config()` |
| Surplus compute | Sonnet | MEDIUM | `session_config.build_surplus_config()` |
| Foreground (user) | User's choice | User's choice | `/model` command or MCP |

### Research-Based Assessment

Per SWE-bench data and community analysis (April 2026):
- **Medium** is optimal for 80-90% of agentic coding (15-20% improvement over no-thinking)
- **High/Max** shows diminishing returns except for cross-file refactoring and logical debugging
- On simple tasks, High effort over-analyzes and over-engineers

### Observations

- **Deep reflection at HIGH seems correct** — reflections are multi-file analysis
- **Task execution at MEDIUM seems correct** — most tasks are standard coding
- **Surplus at MEDIUM seems correct** — brainstorms don't benefit from deep reasoning
- **Light reflection at LOW seems correct** — just signal classification
- **Strategic at MAX may be overkill** — only Opus already has high baseline logic; MAX adds nuance but at significant cost/latency. Worth testing HIGH instead.

### Quick Wins (Code Changes Only)

1. Test strategic reflections at HIGH instead of MAX — change one line in `_effort_for_context()`
2. Consider MEDIUM for some deep reflections that are routine (e.g., daily memory flush)

### V4 Path: Per-Call-Site Effort in Routing Config

To enable per-call-site effort tuning:
1. Add `effort_override: str | None` field to `CallSiteConfig` in `routing/types.py`
2. Update `model_routing.yaml` schema to accept `effort:` per call site
3. Have the CC invoker read effort from the routing config when dispatching
4. Track effort level in `call_site_last_run` for empirical analysis

This would allow: Low for fact extraction (#9), Medium for standard review (#17),
High for adversarial review (#20) — without changing application code.

---

## Last Reviewed
2026-04-12 — added effort level section with current assignments, research assessment, and V4 path.
2026-03-14 — added GPT-5.4 (computer use, compaction training, agentic focus);
noted co-orchestrator potential and disagreement gate use case for Genesis.
2026-03-03 — added Grok 4, GPT-5.2, GPT-5 Nano/Mini, Qwen 3.5 Plus,
Qwen3-Max-Thinking; added Mistral/Groq/OpenRouter free tiers; updated
Gemini entries; cross-referenced model routing registry
