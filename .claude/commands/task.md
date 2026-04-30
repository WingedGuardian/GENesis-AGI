---
name: task
description: >
  Guided task intake — interviews the user, writes a plan following
  TASK_INTAKE.md format, validates structure, then submits to the
  autonomous task executor.
---

# Task Submission — Guided Intake

You are conducting a task intake interview following the TASK_INTAKE.md
format (`src/genesis/identity/TASK_INTAKE.md`).

## Process

1. Read the user's request: $ARGUMENTS
2. **Triage first**: Is this multi-step, time-consuming, background-appropriate?
   If not, handle inline — say so and do it directly.
3. Gather requirements (one question at a time, don't over-interview):
   - Desired outcome and success criteria (specific + testable)
   - LLM provider preference (OpenAI/Anthropic/other) with SDK + env var + model
   - Risks and failure modes (specific, not vague)
   - Deliverable format and where output goes
   - Any URLs, file paths, API details, or credentials the executor needs
4. Enter plan mode and write the plan to `~/.genesis/plans/`
5. Plan structure — `task_submit` **enforces** these four sections (rejects if missing):
   - `## Requirements`
   - `## Steps` (from executor CC session's perspective)
   - `## Success Criteria` (testable by the executor)
   - `## Risks and Failure Modes` (specific)

   Also include these sections (best practice, caught by the LLM plan reviewer):
   - `## Context`
   - `## Deliverable Format`
   - `## Quality Checks` (achievable in the executor's environment)
   - `## Constraints`
6. After user approves the plan:
   a. Call `intake_complete(session_description=<brief description>)` MCP tool
      to generate a one-time intake token
   b. Call `task_submit(plan_path=<path>, description=<description>, intake_token=<token>)`
      MCP tool with the token from step 6a
   The token enforces that submissions went through this intake process.
   It expires after 2 hours and can only be used once.

## Critical Rules

- **Steps are for the EXECUTOR** — what the background CC session will DO,
  not what the user does. Write from the executor's perspective.
- **Success criteria must be executor-testable** — if the executor can't verify
  it (e.g., browser testing without a display server), explicitly note it as
  "deferred to user" and provide criteria the executor CAN check.
- **Concrete references only** — include actual URLs, file paths, API details.
  Never say "study the repo" — say "read https://github.com/org/repo/tree/main/path".
- **Quality Checks must not contradict environment constraints** — don't require
  "run the app and test manually" when the executor runs headless in a container.
- **Specify LLM provider explicitly** — SDK name, env var name, model name.
  Don't say "use an LLM" — say "use the `openai` SDK, `OPENAI_API_KEY`, model `gpt-4o-mini`".
- **Validation is enforced** — `task_submit` will reject plans missing
  `## Requirements`, `## Steps`, `## Success Criteria`, or
  `## Risks and Failure Modes`. Get it right before submitting.
