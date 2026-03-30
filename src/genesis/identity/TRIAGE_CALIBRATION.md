---
version: "1.0"
description: >
  Few-shot calibration examples for the triage classifier.
  Generated at 2026-03-29T03:00:14.103620+00:00 by claude-3-5-sonnet-20240620.
---

## Few-Shot Examples

| # | Scenario | Depth | Rationale |
|---|----------|-------|-----------|
| 1 | User asks 'what time is it?' — Genesis responds with the current time. | 0 | Trivial factual response with no learning value or reusable insights. |
| 2 | User asks to rename a variable across a file. Genesis uses a single Edit tool and succeeds. | 1 | Simple mechanical task with no iteration or obstacles. Only note completion. |
| 3 | User asks Genesis to explain a module's architecture. Genesis reads several files and produces a summary, but the summary lacks depth and misses key components. | 2 | Exploration task with multiple steps but no significant obstacles. Light analysis of approach and gaps is valuable for future improvements. |
| 4 | User requests a new feature with tests. Genesis writes code, runs tests, fixes failures, and iterates until all tests pass. The response includes a summary of the changes and test results. | 3 | Multi-step implementation with iteration and debugging. Full outcome analysis is required to capture what worked and the iterative process. |
| 5 | User asks Genesis to investigate a performance bottleneck in a distributed system. Genesis performs a multi-step diagnostic analysis, identifies root causes, proposes multiple solutions, and documents workarounds for future reference. The response includes detailed insights and reusable patterns. | 4 | Complex diagnostic task with obstacles, multiple approaches, and workaround extraction. Detailed analysis and reusable insights are critical for future similar tasks. |
| 6 | User says 'thanks' — Genesis acknowledges. | 0 | Social exchange with no learning content. |
| 7 | User asks Genesis to draft a design doc for a new API. Genesis produces a document with multiple sections, but the user provides iterative feedback leading to revisions and refinements. The final document includes design decisions and trade-offs. | 3 | Creative task with iterative feedback and refinements. Full outcome analysis is valuable to capture the evolution of the design and key decisions. |
| 8 | User asks Genesis to analyze gaps in the current hooks structure, identifying hardcoded paths and potential issues. Genesis provides a detailed gap analysis with insights into what worked and what needs further investigation. | 4 | Complex diagnostic task involving multi-step reasoning, outcome extraction, and identification of reusable insights and workarounds. |

## Calibration Rules

- Depth 0 (SKIP): Trivial interactions, social exchanges, or factual responses with no learning value or reusable insights.
- Depth 1 (QUICK_NOTE): Simple mechanical tasks with a single tool and no iteration, obstacles, or reusable insights. Only note completion.
- Depth 2 (WORTH_THINKING): Exploration, analysis, or creative tasks with multiple steps but no significant obstacles or iterations. Light analysis of approach and gaps is valuable.
- Depth 3 (FULL_ANALYSIS): Multi-step tasks with iteration, debugging, or implementation workflows. Full outcome analysis is required to capture what worked, the iterative process, and key decisions.
- Depth 4 (FULL_PLUS_WORKAROUND): Complex tasks with obstacles, multiple approaches, diagnostic workflows, or workaround extraction. Detailed analysis and reusable insights are critical for future similar tasks.
- Diagnostic or architectural analysis tasks involving multi-step reasoning, outcome extraction, and identification of reusable insights or workarounds should be classified at Depth 4.
- Tasks involving iterative testing, debugging, or refactoring with clear outcomes and key decisions should be classified at Depth 3 unless obstacles or workarounds are encountered.
- If a task involves gap analysis, comparison against reference materials, or multi-step outcome extraction, it should generally be classified at Depth 3 or 4 depending on the complexity and reusability of insights.
