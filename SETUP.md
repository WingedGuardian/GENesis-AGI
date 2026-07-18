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
GOOGLE_API_KEY=your-key-here
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

## Backups

Backups run every 6 hours via the `genesis-backup.timer` systemd user unit
and split into two tiers:

- **Tier 1** (git → GitHub): Memory files, configs, secrets (~1MB). Automatic.
- **Tier 2** (smbclient → NAS/remote): Qdrant snapshots, SQL dumps (~200MB+). Opt-in.

Tier 2 keeps large binary files off GitHub (which has a 100MB file limit).
Without Tier 2 configured, large files are local-only and the dashboard shows
a yellow warning.

To configure Tier 2, add to `secrets.env`:
```
GENESIS_BACKUP_NAS="//your-nas-ip/share-name"
GENESIS_BACKUP_NAS_USER=username
GENESIS_BACKUP_NAS_PASS=password
```

Requires `smbclient` (`sudo apt-get install smbclient`).

Off-site snapshots are written under `<share>/Genesis/<host>/`, where `<host>`
defaults to the machine's hostname. **If you back up two machines that share a
hostname to the same NAS, give each a distinct label** or their retention prunes
will delete each other's snapshots:
```
GENESIS_BACKUP_NAS_HOST=this-machine-label
```
(`restore.sh` reads the same variable to find the source snapshot dir.)

Bootstrap installs the timer's unit files but does **not** enable them —
scheduling a backup that silently leaves your database local-only would give a
false sense of safety. Once `GENESIS_BACKUP_REPO` and
`GENESIS_BACKUP_PASSPHRASE` are set (and Tier 2, if you want off-site copies of
the large payloads), enable it deliberately and verify one run:

```bash
systemctl --user enable --now genesis-backup.timer
systemctl --user start genesis-backup.service   # fire one run now
cat ~/.genesis/backup_status.json               # expect "success":true
```

Or manage it from the dashboard **Backup** tab (Settings → Backup): the schedule
toggle + interval (every 3h / 6h / 12h / daily), a **Run Now** button, and both
destinations (GitHub Tier-1 and the off-site Tier-2) with live health. The tab
drives the same `genesis-backup.timer` unit over `systemctl --user` — it does not
use crontab.

> **Migrating from an old crontab-based schedule?** Earlier installs scheduled
> backups with a `crontab` line (`… /scripts/backup.sh …`). The systemd timer and
> a leftover cron line will BOTH fire, running two backups that race on the same
> repo. When you enable the timer, remove any legacy line:
> ```bash
> crontab -l | grep -v 'scripts/backup.sh' | crontab -
> ```

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
- **Services can't find `claude` (nvm users)**: after changing your active Node version under nvm, re-run `scripts/update.sh` to re-render the systemd units' baked Claude Code path (`claude: not found` in the service journal is the tell). See `docs/reference/cc-compatibility.md`.

## Architecture

See `docs/architecture/` for detailed design documents, or `.claude/docs/architecture-index.md` for a quick reference.
