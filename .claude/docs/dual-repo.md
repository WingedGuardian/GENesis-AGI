# Repo Architecture

## Three-Repo Model

| Repo | Role | Who |
|------|------|-----|
| **GENesis-AGI** (public) | Primary development. All core work, PRs, CI. | Everyone |
| **GENesis** (private fork, optional) | Personal customizations: CLAUDE.md with local IPs, career agent, research profiles, private experiments. | Individual devs who want one |
| **genesis-backups** (private) | DB + data backups via cron. | Each install, their own |

## Development Workflow

**Core devs** (write access to `GENesis-AGI`):
1. Clone `GENesis-AGI`
2. Run `scripts/setup-local-config.sh` to create `~/.genesis/config/genesis.yaml`
3. Develop normally on branches, PR directly to `GENesis-AGI`
4. No sanitizer needed — repo is already install-agnostic

**Users** (running Genesis locally):
1. Clone `GENesis-AGI`
2. Run `scripts/setup-local-config.sh`
3. Fix bugs → `genesis contribute <sha>` → sanitized PR to `GENesis-AGI`

## Private Fork Workflow (Optional)

For devs who want version-controlled personal customizations:

```bash
git remote add upstream https://github.com/WingedGuardian/GENesis-AGI.git
git pull upstream main   # sync regularly
```

Keep custom changes (CLAUDE.md additions, private configs) as local commits
or a `private` branch. Never push custom commits to upstream — contribute via
PRs only.

## Machine-Specific Config

User-specific values live in `~/.genesis/config/genesis.yaml`, NOT in the repo:
- Service URLs (Ollama, LM Studio)
- Timezone
- GitHub identity
- Local module configs (`~/.genesis/config/modules/`)
- Research profiles (`~/.genesis/config/research-profiles/`)

Generate with: `./scripts/setup-local-config.sh`
Reference template: `config/genesis.yaml.example`

## CI Leak Detector

`.github/workflows/ci.yml` includes a `leak-detector` job that blocks PRs
containing user-specific content:
- Hardcoded timezones (`America/New_York`)
- Personal paths (`/home/<user>/genesis`)
- Private GitHub repo references
- Secrets (`detect-secrets`)
- Personal email addresses

The detector's exclusion list mirrors `prepare-public-release.sh` SCAN_EXCLUDES.

## Legacy Scripts

`scripts/prepare-public-release.sh` is kept as a reference for the sanitizer
exclusion patterns. `scripts/push-public-release.sh` is no longer used for
regular releases — development happens on the public repo directly.
