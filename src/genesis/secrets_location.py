"""Where the credentials file lives: one resolver for every reader.

CONTRACT: standard library only, and no ``genesis.*`` imports. Scripts that
run before the package is importable (hooks, ``gmodel``, the session-context
script) load THIS FILE by path, so it must run with no package context.
``scripts/lib/secrets_path.sh`` is the bash twin, and
The service-preparation PR adds a third copy inside the standalone
``guardian/cred_integrity.py``. The host pipes that source into the container's
system python, so it cannot import this module. The resolver tests currently
compare Python and Bash over the same states.

Order, first match wins:

1. ``SECRETS_PATH``, when set: an explicit override, used as given.
2. ``$HOME/.config/genesis/secrets.env``, when it exists (the new location).
3. A legacy file, when one exists: ``<repo root>/secrets.env``, then
   ``$HOME/genesis/secrets.env``. The second catches a worktree or a
   non-canonical checkout whose own root holds no file. This fallback is
   permanent: an install that skips several releases still finds its keys.
4. Nothing exists: ``$HOME/.config/genesis/secrets.env``. Bootstrap, restore
   and the dashboard create its parent directory when needed.

``XDG_CONFIG_HOME`` is deliberately ignored: the systemd user manager does
not carry it, so honouring it would let a session and the server resolve
different files.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

BASENAME = "secrets.env"


def new_location(home: Path) -> Path:
    """The credentials file's home outside every checkout."""
    return home / ".config" / "genesis" / BASENAME


def legacy_locations(home: Path, repo_root: Path) -> list[Path]:
    """The in-checkout locations, most specific first, without duplicates."""
    out: list[Path] = []
    for candidate in (repo_root / BASENAME, home / "genesis" / BASENAME):
        if candidate not in out:
            out.append(candidate)
    return out


def resolve(*, environ: Mapping[str, str], home: Path, repo_root: Path) -> Path:
    """The file a process with this environment, home and checkout reads."""
    override = environ.get("SECRETS_PATH")
    if override:
        return Path(override).expanduser()
    new = new_location(home)
    if new.is_file():
        return new
    for candidate in legacy_locations(home, repo_root):
        if candidate.is_file():
            return candidate
    return new
