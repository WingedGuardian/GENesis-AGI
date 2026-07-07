"""Extracted SurplusScheduler job bodies.

Each module groups related jobs; ``SurplusScheduler`` keeps every original
method name as a thin delegate, so APScheduler job callables, runtime wiring,
and tests are unaffected by the extraction.

- ``gates``    — cooldown-gated enqueue jobs (``schedule_*`` / brainstorm check)
- ``runners``  — direct-run jobs delegating to wired components
- ``dream``    — the weekly dream-cycle pair (clustering + daily drain)
- ``gitnexus`` — GitNexus reindex + CLAUDE.md block strip
"""
