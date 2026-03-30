# Agent Zero Architecture Reference

Agent Zero lives at `~/agent-zero`. Genesis extends it — consult this doc when
working on tools, guardrails, or integrations.

## Key Components

- **`agent.py`** — Core `Agent` class, tool timeouts (`TOOL_TIMEOUTS`), output
  truncation (`TOOL_OUTPUT_MAX_CHARS = 50_000`)
- **`initialize.py`** — Agent init: chat/utility/embedding/browser model config
- **`models.py`** — LLM provider abstraction (ModelConfig, ModelType)
- **`run_ui.py`** — Flask + Socket.IO ASGI app (port 50001)
- **`python/tools/`** — Tool implementations (each extends `python.helpers.tool.Tool`)
- **`python/helpers/`** — Shared infra: shell, guardrails, vector DB, MCP, Git
- **`prompts/`** — Prompt templates (dotted naming, e.g. `agent.system.main.md`)

## Hardening / Resource Limits

Respect these in any new tools. Log blocks via `log_guardrail_block()`.

| Resource | Limit |
|----------|-------|
| Shell stdout | 512KB |
| Tool output to LLM | 50K chars |
| Default tool timeout | 60s |
| Code execution timeout | 180s |
| Subordinate agent timeout | 300s |
| File read / write max | 10MB / 2MB |

Key code locations:
- `python/helpers/guardrails.py` — Guardrail logging + prompt injection detection
- `python/tools/code_execution_tool.py` — Command allowlist, blocked dunders, stdout cap
- `agent.py` — Tool timeout map and output truncation constants
