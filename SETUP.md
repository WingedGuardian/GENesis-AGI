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

> **Two install paths.** This bare-metal `bootstrap.sh` quick-start targets
> developers and restores: it configures infrastructure and expects you to add
> keys manually (steps above). For a guided, container-based install that walks
> you through API keys, profile, and channels interactively, use
> `scripts/host-setup.sh` (see the README) — it runs first-run onboarding
> automatically, and you can re-trigger it with `/setup` if it doesn't start.

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
   TELEGRAM_ALLOWED_USERS=your_numeric_user_id   # comma-separated for multiple
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

**Extra directories (optional).** To keep install-local data no other backup
section knows about, list directories under your home directory, separated by
`:`:
```
GENESIS_BACKUP_EXTRA_DIRS=~/.genesis/analytics:~/.genesis/tools/my-tool
GENESIS_BACKUP_EXTRA_EXCLUDES=derived:scratch
```
Each directory becomes one encrypted archive, `extra/<name>.tar.gpg`. It goes to
the off-site tier only, never the git tier. Every run builds these archives from
scratch: a snapshot holds only what that run archived, and nothing is carried
over from an earlier run. Rebuildable caches (`.venv`, `node_modules`,
`__pycache__`, …) are always excluded, and `GENESIS_BACKUP_EXTRA_EXCLUDES` adds
more tar exclude patterns (wildcards allowed), matched at any depth.

A listed directory is skipped with a warning when it:
- is relative, or outside your home directory;
- is, or runs through, a symlink (list the real directory instead);
- is inside or contains another entry, the backups repo, the backup temp dir, or
  a local off-site root;
- overlaps a path the core backup already restores (for example `~/.genesis`
  itself, or anything inside the repo);
- is missing, or tar cannot read it;
- would be emptied by an exclude pattern;
- could not be extracted by a restore on this machine: restore needs a Python whose
  `tarfile` has the 2025 extraction-filter fixes (CPython 3.12.11 or later, or a
  distribution backport). The machine you restore onto needs one too.

A skipped directory is simply absent from that snapshot; older snapshots keep it
until retention drops them. None of this fails the backup, but each skip, and
each archive that fails to upload, marks the off-site copy `partial`
(`offsite_confirmed: false`, `extras_complete: false`) and sends the off-site
alert, again whenever the set of missing directories changes. The core snapshot is still marked complete
(`offsite_core_complete: true`), retention still runs, and a later failure of the
core off-site copy still alerts on its own. The snapshot's `COMPLETE` marker
lists the extra archives it holds and the listed directories it skipped, so a
restore can tell "none" apart from "could not list them" and can name what a
snapshot is missing; `.extra-manifest` in the backups checkout does the same for a
restore that runs without an off-site pull. Either way, restore only restores
archives that list names: a leftover archive in `extra/` is never restored. Backup
test-extracts each archive the way restore will (file contents left out), so it
knows which members a restore would refuse, such as a symlink that leads outside
the directory or a FIFO. Such a directory is still archived, but recorded as
partial: the off-site copy is reported incomplete and the alert names it. Exclude
those members with `GENESIS_BACKUP_EXTRA_EXCLUDES`. A file that changes while it is being archived (tar exit 1)
is kept but may be torn, and the log says so; stop a writer whose files must be
consistent, or exclude them. Without an off-site tier the archives stay local
only, in the backups checkout, and no off-site alert applies.

`restore.sh` puts each directory back as a whole: it unpacks the archive next to
the destination and renames it into place, so directory permissions and empty
directories come back too. An existing non-empty directory is replaced only with
`--force`, and the old one is kept beside it as `<dir>.pre-restore-<timestamp>`,
never deleted; without `--force` that directory is skipped and recorded as a
failure. Members that use `..`, a special file, or a link pointing outside the
restored directory are refused one by one and the rest restores. A whole archive
is refused when its destination is a symlink, when its path runs through a
symlink that leads outside your home directory, or when it overlaps a core
restore path. Every refusal is recorded. After an off-site pull, restore takes
exactly the archives that snapshot's `COMPLETE` marker lists.

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

The status file is also the machine-readable proof used by `scripts/update.sh`.
Each run records a unique `run_id`, `db_integrity_status`,
`sqlite_backup_verified`, `failure_class`, and `failure_stage`. An update aborts
before pulling code or running migrations unless that exact run proves a healthy
source and a round-trip-verified SQLite artifact. Failures outside SQLite (for
example Qdrant, GitHub push, or an off-site target) do not invalidate that
artifact, so the update may continue, but it prints an untruncated
`BACKUP FAILED — UPDATE CONTINUING IN DEGRADED MODE` banner and records
`backup:<failure_stage>` in update history.

If SQLite is corrupt, backup preserves the last-known-good artifacts, writes
`~/.genesis/db_quarantine.json`, and stops both long-lived database writers.
New canonical read/write connections and the watchdog honor that quarantine.
Install a verified replacement with `scripts/restore.sh --database-only`; a
healthy replacement inode clears the stale quarantine automatically.

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

### Backup ↔ restore mutual exclusion

`backup.sh` and `restore.sh` coordinate through a single lock
(`~/.genesis/locks/backup-restore.lock`) so the 6-hourly timer can never
snapshot a database that a restore is mid-way through rebuilding:

- A **backup** that finds the lock held (a restore is running) **skips** that
  run — it logs `SKIPPED: backup-restore lock held …` to the journal and exits
  cleanly. The next scheduled run backs up normally. (The dashboard's Backup
  status keeps showing the prior run until then.)
- An **update-triggered backup** never treats lock contention as a successful
  skip. It writes a `lock_busy` failure for that run and the update aborts.
- A **restore** that finds the lock held (a backup is running) **waits** up to
  `GENESIS_RESTORE_LOCK_WAIT` seconds (default **300**), then aborts naming the
  holder. A first full off-site backup can take longer than 300s, so for an
  unattended disaster-recovery restore during a backup window, raise it:
  ```bash
  GENESIS_RESTORE_LOCK_WAIT=1800 scripts/restore.sh …
  ```

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
- **Services can't find `claude` (nvm users)**: if you change your active Node version under nvm, the systemd units' baked Claude Code path can go stale (`claude: not found` in the service journal is the tell). Repair with `./scripts/bootstrap.sh --force` then `systemctl --user restart genesis-server` (a routine `scripts/update.sh` that pulls new commits also re-renders them). See `docs/reference/cc-compatibility.md`.

## Architecture

See `docs/architecture/` for detailed design documents, or `.claude/docs/architecture-index.md` for a quick reference.
