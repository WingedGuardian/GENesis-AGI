# Contributing to Genesis

Thanks for your interest in Genesis. This guide covers everything you need to get started.

## Quick start

Genesis runs inside an Incus container. The fastest way to get a dev environment:

```bash
# On a fresh Linux VM (Ubuntu 22.04+, Debian 13+)
git clone https://github.com/WingedGuardian/GENesis-AGI.git ~/genesis-setup
cd ~/genesis-setup
./scripts/host-setup.sh

# Inside the container
incus exec genesis --user 1000 --env HOME=/home/ubuntu -- bash
cd ~/genesis
source .venv/bin/activate
```

## Development workflow

```bash
# Activate the venv (required for all Python work)
source ~/genesis/.venv/bin/activate

# Lint
ruff check .

# Run tests
pytest -v

# Both (run before every commit)
ruff check . && pytest -v
```

## Making changes

### Two contribution paths

**For bug fixes while running Genesis** — use the pipeline. Genesis detects `fix:` commits and offers to contribute them upstream. Just accept. The pipeline auto-creates your fork, sanitizes the diff (stripping personal paths, secrets, PII), and opens the PR against `main`. See [`.claude/docs/your-genesis.md`](.claude/docs/your-genesis.md).

**For features or larger changes** — use the standard open-source flow below.

### Standard flow (features, larger changes)

1. **Fork the repo** and clone your fork
2. **Create a branch**: `git checkout -b <scope>/<description>` (e.g., `feat/memory-recall-timeout`)
3. **Make your changes** — target ~600 LOC per file, hard cap 1000
4. **Run lint + tests**: `ruff check . && pytest -v`
5. **Commit** with a conventional prefix: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`
6. **Open a PR** against `main`

## Commit conventions

```
feat(memory): add semantic deduplication to recall
fix(dashboard): uptime counter timezone offset
refactor(routing): extract fallback chain logic
docs: update install instructions for Debian 13
test(guardian): add probe timeout coverage
chore: pin wsproto dependency
```

Keep the subject line under 72 characters. Scope is optional but helpful.

## Code style

- **Python 3.12** — use modern syntax (type unions with `|`, etc.)
- **Ruff** for linting and formatting — config is in `pyproject.toml`
- **No unnecessary abstractions** — three similar lines > a premature helper
- **Catch specific exceptions** before generic `except Exception`
- **Log at appropriate levels** — ERROR for operational failures, DEBUG for tracing

## Architecture

The architecture docs in [`docs/architecture/`](docs/architecture/) are the primary reference:

- [`genesis-v3-vision.md`](docs/architecture/genesis-v3-vision.md) — Core philosophy
- [`genesis-v3-autonomous-behavior-design.md`](docs/architecture/genesis-v3-autonomous-behavior-design.md) — Primary design reference
- [`genesis-v3-build-phases.md`](docs/architecture/genesis-v3-build-phases.md) — Build plan and phase history

## What to work on

- Issues labeled [`good first issue`](https://github.com/WingedGuardian/GENesis-AGI/labels/good%20first%20issue) are scoped for new contributors
- Issues labeled [`help wanted`](https://github.com/WingedGuardian/GENesis-AGI/labels/help%20wanted) are open for community contribution
- Check [Discussions](https://github.com/WingedGuardian/GENesis-AGI/discussions) for ideas and design conversations

## Questions?

Open a [Discussion](https://github.com/WingedGuardian/GENesis-AGI/discussions) — that's the best place for questions, ideas, and design conversations. Issues are for bugs and concrete feature requests.
