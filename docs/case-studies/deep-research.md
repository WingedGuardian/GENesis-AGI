# Structured Research That Actually Researches

Deep research features are everywhere now. ChatGPT has one. Perplexity has one.
Gemini has one. They all take a prompt, search the web, and return a summary.
The problem is that searching the web and summarizing results is not research.
Research means collecting primary data, reading full sources, cross-referencing
across backends, and filtering by constraints the user actually cares about.

Genesis does research differently.

---

## The Scenario

Same structured research prompt, same day, four AI systems:

- ChatGPT Deep Research (OpenAI)
- Perplexity Pro (Perplexity AI)
- Gemini Deep Research (Google)
- Genesis

The task: analyze 11 job market segments across 5 dimensions each (supply signal,
demand analysis, quadrant placement, competitive landscape, gap analysis), plus
company culture deep-dives for the top 15-20 organizations identified. Output
format specified. Sources required. Real data, not generic summaries.

This is the kind of research task that takes a human analyst 2-3 days. The
question: which tool gets closest to what a thorough human researcher would
produce?

---

## The Results

**Gemini Deep Research** — No output. After 4+ hours of repeated attempts, it
never produced a result.

**ChatGPT Deep Research (C+)** — Produced output, but the wrong output. Ignored
explicit prompt constraints ("not needed: career coaching language, generic
job-search advice") and filled 40% of the response with recruiter hooks, LinkedIn
templates, and Gantt charts. Zero URLs to actual job listings. Vague supply
estimates with asterisks. Every finding is a secondhand summary of a summary.

**Perplexity Pro (B+)** — Best of the three commercial tools. Numbered citations
for every claim. Platform-specific supply numbers. Actual company names and JD
excerpts. But: passive research only. No active data collection, no company
culture analysis, no filtering by real-world constraints like remote policy or
management quality.

**Genesis (A)** — 6,409 live job listings from 39 companies via direct ATS API
queries. Four simultaneous search backends. 50+ full job descriptions read and
analyzed. 17 company culture dossiers via Glassdoor, Blind, and news triangulation.
6 culture ruleouts identified that all other tools recommended as top targets.
A 205% YoY growth segment found that was invisible to tools trained on older data.
Completed in under 45 minutes.

---

## How It Works

Five capabilities that chatbot-style research tools don't have:

**Live API data collection.** Genesis queried Greenhouse, Ashby, and Lever APIs
directly. These are the three major applicant tracking systems used by tech
companies. The APIs are public and free, but no chatbot queries them because
they're structured APIs, not web pages. Result: real-time primary source data
that doesn't exist in any search index.

**Multi-backend search triangulation.** The same queries ran across four search
backends simultaneously: Tavily (AI-optimized), Exa (neural/semantic), SearXNG
(meta-search with site filters), and Claude Code WebSearch (general). Each
backend has different retrieval biases. Using four at once finds different results,
and the divergence between what each backend returns is itself informative.

**Primary source analysis.** Genesis fetched and read 50+ actual job description
pages. Not search result snippets. Full JD text. A search snippet says "AI
Engineer, experience required." The actual JD might say "We welcome self-taught
engineers with equivalent experience." That nuance only comes from reading the
source.

**Active culture investigation.** Genesis produced dossiers for 17 companies by
triangulating Glassdoor ratings, Blind posts, recent news, and employee departure
patterns. One company that appeared as a top target in both Perplexity's and
ChatGPT's reports had reviews describing "the most toxic culture I've ever seen."
Another had disbanded its ethics team. These ruleouts don't appear in any search
result. They require active investigation, then correlating findings with the
job listings.

**Source-tagged synthesis.** After producing its own independent report, Genesis
ingested the ChatGPT and Perplexity outputs and produced a synthesis where every
claim is attributed to which tool supports it, and every divergence is flagged
explicitly. "Genesis ranks this cluster #1; ChatGPT ranks it mid-pack; Perplexity
treats it as emerging. Genesis's argument: 'growing 205% YoY but invisible to
tools trained on historical data.'"

---

## Why It Matters

The gap between Genesis and the commercial tools on this task isn't about the
underlying model being smarter. Claude, GPT-4o, and Gemini are comparable in raw
reasoning. The gap is architectural:

- **Tools over training data.** A system that can query an API gets real-time data.
  A model relying on training data gets whatever was in the index when it was last
  updated.
- **Multiple search backends over one.** Each backend has different biases. Using
  several at once is like polling multiple experts with different blind spots.
- **Reading primary sources over summarizing snippets.** The nuance lives in the
  full document, not in a 200-character search result.
- **Active investigation over passive retrieval.** Culture research, policy
  verification, and ruleout analysis require proactive investigation. They can't
  be answered by searching and summarizing.

For structured research tasks that require active data collection, multi-source
triangulation, and domain-specific filtering, an agentic system with tool access
produces categorically better output than a single-model deep research feature.
The architecture matters more than the model.
