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

### Avoiding install-specific data

Genesis scans contributions for machine-specific details so nothing personal
reaches the public repo. Two things keep your PRs clean:

- **Use RFC 5737 ranges for example IPs** — `192.0.2.0/24`, `198.51.100.0/24`,
  `203.0.113.0/24` (and `2001:db8::/32` for IPv6). These are reserved for
  documentation and are never flagged. Real private-range addresses (any
  `10.x`, `172.16–31.x`, `192.168.x`, `100.64–127.x`, or `fc00::/7` ULA) trigger
  a **non-blocking CI warning** so you can swap them — they won't block your PR,
  but please use the doc ranges.
- **Keep home paths generic** — write `/home/<user>/…` or `~/…`, not a real
  username. Absolute `/home/<user>/genesis` and CC project-dir slugs are flagged.

Install-specific *exact* values (private hostnames, repo names, account IDs) are
matched only from a per-install fingerprint file and a private CI secret — never
from anything tracked in this repo — so forks and outside contributors are never
gated on values they can't know.

### Standard flow (features, larger changes)

1. **Open an issue or Discussion first** — describe what you want to change and why.
   This lets us confirm it fits the project direction before you write code.
   Skip this for typo fixes or small doc corrections.
2. **Fork the repo** and clone your fork
3. **Create a branch**: `git checkout -b <scope>/<description>` (e.g., `feat/memory-recall-timeout`)
4. **Make your changes** — target ~600 LOC per file, hard cap 1000
5. **Run lint + tests**: `ruff check . && pytest -v`
6. **Commit** with a conventional prefix: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`
7. **Open a PR** against `main` — link the issue with a **closing keyword**:
   `Closes #123` (or `Fixes`/`Resolves #123`) in the PR description. A bare `#123`
   only cross-links; a closing keyword makes GitHub close the issue automatically
   when the PR merges — and closing only fires on a merge to the **default branch**.

PRs without a prior issue or discussion may be closed if the change wasn't
discussed first. All PRs require a maintainer review before merging.

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
- Each issue carries an **`area:*`** label (memory, dashboard, runtime, guardian,
  autonomy, channels, knowledge, eval) so you can find work in a domain you know,
  and an environment label: **`needs-genesis-instance`** means you'll want a running
  Genesis to reproduce/validate; without it you can generally start from a clone.
- Check [Discussions](https://github.com/WingedGuardian/GENesis-AGI/discussions) for ideas and design conversations

## Questions?

Open a [Discussion](https://github.com/WingedGuardian/GENesis-AGI/discussions) — that's the best place for questions, ideas, and design conversations. Issues are for bugs and concrete feature requests.
