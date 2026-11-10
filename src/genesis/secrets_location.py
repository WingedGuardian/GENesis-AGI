"""Where the credentials file lives: one resolver for every reader and writer.

CONTRACT: standard library only, and no ``genesis.*`` imports. Scripts that
run before the package is importable (hooks, ``gmodel``, the session-context
script) load THIS FILE by path, so it must run with no package context.
``scripts/lib/secrets_path.sh`` is the bash twin. A third copy lives inside
the standalone ``guardian/cred_integrity.py``, which the host pipes into the
container's system python, so it cannot import this module.
``tests/test_secrets_location.py`` runs all three over the same states.

Order, first match wins:

1. ``SECRETS_PATH``, when set: an explicit override, ``~`` expanded.
2. ``$HOME/.config/genesis/secrets.env``, when it exists (the home config).
3. A legacy file, when one exists: ``<repo root>/secrets.env``, then
   ``$HOME/genesis/secrets.env``. The second catches a worktree or a
   non-canonical checkout whose own root holds no file. This fallback is
   permanent: an install that skips several releases still finds its keys.
4. Nothing exists: ``$HOME/.config/genesis/secrets.env``. Bootstrap, restore
   and the dashboard create its parent directory when needed.

``XDG_CONFIG_HOME`` is deliberately ignored: the systemd user manager does
not carry it, so honouring it would let a session and the server resolve
different files. The home directory is ``Path.home()``, the same ``$HOME`` the
bash twin and the secrets guard read.

A candidate counts as present when it is a regular file, and also when it may
exist but cannot be checked (a directory on its path is not searchable). That
keeps the reader on the file it cannot open, so it fails loudly instead of
silently loading an older legacy copy; all three copies agree on this.

The home config location lands together with the services, guard and restore
paths that serve it, so no reader can select a file nothing yet protects.
"""

from __future__ import annotations

import errno
import stat
from collections.abc import Mapping
from pathlib import Path

BASENAME = "secrets.env"


def home_config_location(home: Path) -> Path:
    """The credentials file's home outside every checkout."""
    return home / ".config" / "genesis" / BASENAME


def legacy_locations(home: Path, repo_root: Path) -> list[Path]:
    """The in-checkout locations, most specific first, without duplicates."""
    out: list[Path] = []
    for candidate in (repo_root / BASENAME, home / "genesis" / BASENAME):
        if candidate not in out:
            out.append(candidate)
    return out


def _present(path: Path) -> bool:
    """A regular file, or a path that may hold one but cannot be checked."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        return exc.errno != errno.ELOOP


def resolve(*, environ: Mapping[str, str], repo_root: Path, home: Path | None = None) -> Path:
    """The file a process with this environment and checkout reads or writes."""
    override = environ.get("SECRETS_PATH")
    if override:
        return Path(override).expanduser()
    home = Path.home() if home is None else home
    preferred = home_config_location(home)
    if _present(preferred):
        return preferred
    for candidate in legacy_locations(home, repo_root):
        if _present(candidate):
            return candidate
    return preferred
