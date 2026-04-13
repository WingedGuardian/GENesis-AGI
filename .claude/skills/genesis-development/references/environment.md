# Genesis Environment Patterns

How Genesis's infrastructure is organized. User-specific values (IPs,
repo names, machine specs) live in CLAUDE.md per-install.

## Runtime Stack

- **Python 3.12** with a project venv at `~/genesis/.venv`. Activate
  before all Python work.
- **SQLite** (WAL mode, foreign keys ON). DB path resolved via
  `genesis.env.genesis_db_path()` — typically `~/genesis/data/genesis.db`.
- **Qdrant** vector DB on localhost:6333 (systemd service). 1024-dim
  embeddings, UUIDs, delete guard on production data.
- **Embeddings** — cloud-primary (Mistral, DeepInfra). Local Ollama is
  OPTIONAL and install-specific — not every Genesis install has it.
  Configured in `config/model_routing.yaml`. When Ollama is unavailable,
  falls back to cloud providers silently.
- **API keys** in `secrets.env` (chmod 600, gitignored). Missing keys
  cause silent fallback to degraded mode.
- **Env scrub**: `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is NOT used —
  Genesis hooks and MCP servers require inherited API keys.

## Common Commands

```bash
source ~/genesis/.venv/bin/activate               # Required for all Python work
cd ~/genesis && ruff check .                      # Lint all Python
cd ~/genesis && pytest -v                         # Run tests
cd ~/genesis && ruff check . && pytest -v         # Both (do before committing)
curl -s http://localhost:6333/collections | jq .  # Verify Qdrant
python -m genesis serve                           # Standalone server (port 5000)
python -m genesis serve --port 5001               # Custom port
python scripts/setup_claude_config.py             # Regenerate CC config for this machine
./scripts/bootstrap.sh                            # Full setup — venv, config, services, memory
```

## Hosting

Genesis runs standalone: `python -m genesis serve` starts the full runtime
(dashboard, Telegram, OpenClaw endpoint).

**OpenClaw** is supported as a channel gateway. Genesis exposes
`POST /v1/chat/completions` so OpenClaw can route 20+ channels through it.
Config example: `config/openclaw-example.json5`.

## Key Config Files

| File | Purpose |
|------|---------|
| `config/model_routing.yaml` | LLM provider fallback chains + cost budgets |
| `config/autonomy_rules.yaml` | Enforcement spectrum (W1-W7) |
| `config/protected_paths.yaml` | File protection tiers for contribution pipeline |
| `secrets.env` | API keys (gitignored) |
| `~/.genesis/guardian_remote.yaml` | Host VM connection for Guardian |
