---
version: "1.0"
description: >
  Few-shot calibration examples for the triage classifier.
  Generated at 2026-03-30T03:00:15.760319+00:00 by claude-3-5-sonnet-20240620.
---

## Few-Shot Examples

| # | Scenario | Depth | Rationale |
|---|----------|-------|-----------|
| 1 | User asks 'what time is it?' — Genesis responds with the current time. | 0 | Trivial factual response with no learning value or reusable insights. |
| 2 | User asks Genesis to rename a variable across a file. Genesis uses a single Edit tool and succeeds. | 1 | Simple mechanical task with no iteration or obstacles. Only note completion. |
| 3 | User asks Genesis to explain a module's architecture. Genesis reads several files and produces a summary, but the summary lacks depth and misses key components. The response includes a light analysis of gaps for future improvements. | 2 | Exploration task with multiple steps but no significant obstacles. Light analysis of approach and gaps is valuable for future improvements. |
| 4 | [2026-03-29T20:35:31.080755+00:00] The interaction involves a proposal for actionable tasks and a permission change. It requires light analysis to understand the proposed workflows and the impact of the permission change, but no full outcome or delta analysis is needed as it's more about outlining steps and proposing changes. | 3 | Multi-step task involving workflow analysis and permission impact assessment. While not iterative, it requires full outcome analysis to capture key decisions and proposed changes. |
| 5 | [2026-03-29T20:32:01.894006+00:00] This interaction involves a multi-step diagnostic analysis of Genesis's current state and the identification of gaps that need to be addressed for full functionality. The response outlines key components (wired for autonomous action) and areas needing improvement (degraded and blocking elements), warranting detailed outcome analysis. | 4 | Complex diagnostic task with multi-step reasoning, gap identification, and outcome extraction. Detailed analysis and reusable insights are critical for future similar tasks. |
| 6 | [2026-03-29T00:41:41.525950+00:00] Multi-step implementation of a plan to fix gaps involves clear outcomes and diagnostic steps. The response provides a detailed multi-step solution but requires analysis of the execution options for future similar tasks. | 4 | Complex task with obstacles and multiple approaches. Detailed analysis of execution options and reusable insights is critical for future similar tasks. |
| 7 | [2026-03-29T00:14:52.632164+00:00] The interaction involves a multi-step outcome analysis of the current hooks structure and gaps. It includes identifying the issues (hardcoded paths for scripts and servers) and their status (working). The response is detailed enough to capture what worked, but also highlights potential problems that need further investigation or solutions. | 4 | Complex diagnostic task involving multi-step reasoning, outcome extraction, and identification of reusable insights and workarounds. |
| 8 | User says 'thanks' — Genesis acknowledges. | 0 | Social exchange with no learning content. |

## Calibration Rules

- Depth 0 (SKIP): Trivial interactions, social exchanges, or factual responses with no learning value or reusable insights.
- Depth 1 (QUICK_NOTE): Simple mechanical tasks with a single tool and no iteration, obstacles, or reusable insights. Only note completion.
- Depth 2 (WORTH_THINKING): Exploration, analysis, or creative tasks with multiple steps but no significant obstacles or iterations. Light analysis of approach and gaps is valuable.
- Depth 3 (FULL_ANALYSIS): Multi-step tasks with workflow analysis, iteration, debugging, or implementation workflows. Full outcome analysis is required to capture what worked, the iterative process, and key decisions.
- Depth 4 (FULL_PLUS_WORKAROUND): Complex tasks with obstacles, multiple approaches, diagnostic workflows, or workaround extraction. Detailed analysis and reusable insights are critical for future similar tasks.
- Diagnostic or architectural analysis tasks involving multi-step reasoning, gap identification, and outcome extraction should generally be classified at Depth 4.
- Tasks involving iterative testing, debugging, or refactoring with clear outcomes and key decisions should be classified at Depth 3 unless obstacles or workarounds are encountered.
- If a task involves gap analysis, comparison against reference materials, or multi-step outcome extraction, it should be classified at Depth 3 or 4 depending on the complexity and reusability of insights.
- Proposals for actionable tasks or permission changes requiring workflow analysis should be classified at Depth 3 if they involve multi-step reasoning but no significant obstacles.
