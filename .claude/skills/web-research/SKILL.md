---
name: web-research
description: Evidence-driven web and open-source research for questions that require multiple sources, factual verification, comparisons, or an adopt/adapt/build decision. Use for substantial research in foreground sessions, the genesis-researcher subagent, and research-profile background sessions. Skip for a single stable fact or a known URL that only needs fetching.
---

# Web Research

## Frame the decision

State the question, the decision it informs, the required freshness, and what would count as enough evidence. Choose the lightest depth that can answer it:

- **Focused:** one narrow claim or known source. Verify directly and stop.
- **Substantial:** several claims, unfamiliar subject matter, comparison, or recommendation. Decompose into independent questions and synthesize them.
- **Exhaustive:** high-consequence or broad landscape work. Add coverage tracking, competing explanations, and a final gap pass.

Depth follows the task. Do not add an extra pass merely because the word “research” appears.

## Gather evidence

1. Decompose substantial work into questions whose results can be checked independently. Parallelize only independent searches; follow-ups that depend on an earlier result stay sequential.
2. Search with `web_search`, then fetch the sources that carry the actual claim with `web_fetch`. Use browser tools only when interaction or rendered state is necessary.
3. For software capability questions, search for existing implementations before proposing a build. Use `recon_github_search` for public repositories and issues, then `recon_github_read` to inspect repository metadata, trees, and source. GitHub's code-search API requires credentials, so public-only research must discover candidate repositories first and inspect their trees/files rather than silently inheriting operator access. When the foreground `/evaluate` command is available, apply it for the final adopt/adapt/build comparison. In background sessions, perform that comparison directly: assess how each candidate helps, does not help, could help through adaptation, and what Genesis can learn from it; then classify it as adopt, adapt, learn from, or build.
4. Prefer primary sources for product behavior, specifications, code, pricing, and current policy. Use strong secondary sources for context or independent testing. A primary source can establish its own documented behavior; independent corroboration matters when incentives, interpretation, or real-world performance are disputed.
5. Record the URL, publication or update date when relevant, the exact claim supported, and limits or conflicts. A search snippet is a lead, not evidence.

## Evidence standard

- Separate observed facts, source claims, and inference.
- Resolve conflicts by checking recency, authority, methodology, and scope. Report material conflicts that remain.
- Treat a failed search or tool call as unknown. Never turn it into “nothing exists.”
- For recommendations, compare candidates against the stated requirements, including maintenance, license, integration cost, and reversibility.
- Calibrate confidence per major conclusion: high when evidence is direct and current, medium when evidence is indirect or incomplete, low when key uncertainty remains.

## Deliver

Lead with the answer and the decision-relevant evidence. Cite every externally checkable claim near the claim. Include the searches or coverage boundaries only when they explain confidence or remaining uncertainty. End with concrete next actions and open questions when either exists.

For autonomous blocker research, preserve the caller's required JSON result shape after applying this method.
