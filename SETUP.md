# Genesis v3 — Setup Guide

## Prerequisites

- **OS**: Ubuntu 22.04+ (tested on 24.04)
- **Python**: 3.12+
- **Node**: 20.x+
- **Claude Code**: Installed globally (`npm i -g @anthropic-ai/claude-code`)

## Quick Start

```bash
git clone https://github.com/YOUR_USER/genesis.git
cd genesis
./scripts/bootstrap.sh
```

The bootstrap script handles: Python venv, pip install, Claude Code config,
hook launchers, runtime state initialization, and plugin checks.

After bootstrap:
1. Edit `secrets.env` with your API keys (at minimum, one LLM provider)
2. Start Claude Code: `claude` in the genesis directory
3. All hooks and MCP servers activate automatically

## Minimum Viable Setup

Genesis needs at least one LLM provider. The cheapest path:

| Provider | What it gives you | Cost |
|----------|-------------------|------|
| **Gemini** | Triage, light reflection, embeddings | Free tier available |
| **Groq** | Fast inference for background tasks | Free tier available |

Add keys to `secrets.env`:
```
GEMINI_API_KEY=your-key-here
GROQ_API_KEY=your-key-here
```

For full functionality, add keys for: Mistral, OpenRouter, DeepSeek.
See `secrets.env.example` for the complete list with descriptions.

## Claude Code Plugins

Genesis strongly recommends these Claude Code plugins:
- **superpowers** — skills, brainstorming, plans, TDD
- **hookify** — behavioral rule enforcement
- **commit-commands** — git workflow automation

Also helpful: code-review, feature-dev, firecrawl, claude-md-management.

Install via Claude Code's plugin manager.

## Infrastructure (Optional)

- **Qdrant**: Vector search for memory. Install and run on port 6333.
  ```bash
  docker run -d --name qdrant -p 6333:6333 qdrant/qdrant
  ```
- **Ollama**: Local embeddings. Set `OLLAMA_URL` in secrets.env.

Genesis degrades gracefully without these — it falls back to FTS5 text search
and cloud embeddings.

## Post-Install Configuration

### Configure your profile

Edit `src/genesis/identity/USER.md` with your background, expertise, and
preferences. This shapes how Genesis interacts with you.

### Calibrate your voice (optional)

```
/voice calibrate
```

Populates the voice exemplar library with samples of your writing style.

### Set up Telegram (optional)

1. Create a bot via [@BotFather](https://t.me/BotFather)
2. Get your user ID via [@userinfobot](https://t.me/userinfobot)
3. Add to `secrets.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_USER_ID=your_user_id
   ```

## Configuration Files

| File | Purpose |
|------|---------|
| `secrets.env` | API keys, tokens (chmod 600, gitignored) |
| `config/model_routing.yaml` | Which models handle which tasks |
| `config/outreach.yaml` | Timezone, notification preferences |
| `config/autonomy.yaml` | Autonomy levels and approval gates |
| `config/resilience.yaml` | Circuit breaker thresholds |
| `.claude/settings.json` | Hook configuration (portable, tracked in git) |
| `config/cc-global-settings.yaml` | Recommended Claude Code global settings |

## Verify Installation

```bash
source .venv/bin/activate
ruff check .          # lint
pytest -v             # tests
```

## Troubleshooting

- **Hooks not firing**: Run `python scripts/setup_claude_config.py` and restart CC
- **MCP servers not connecting**: Check `.mcp.json` has correct paths. Regenerate with the setup script.
- **Missing venv**: `python3 -m venv .venv && source .venv/bin/activate && pip install -e .`

## Architecture

See `docs/architecture/` for detailed design documents, or `.claude/docs/architecture-index.md` for a quick reference.
