"""Where the credentials file lives: one resolver for every reader and writer.

CONTRACT: standard library only, and no ``genesis.*`` imports. Scripts that
run before the package is importable (hooks, ``gmodel``, the session-context
script) load THIS FILE by path, so it must run with no package context.
``scripts/lib/secrets_path.sh`` is the bash twin, and
``tests/test_secrets_location.py`` runs both over the same states.

The order is today's, unchanged:

1. ``SECRETS_PATH``, when set: an explicit override, ``~`` expanded.
2. Otherwise ``<repo root>/secrets.env``.

This module only gives that rule one home. Locations outside the checkout
are added here together with the services, guard and restore paths that
serve them, so no reader can select a file nothing yet protects, and no
writer can be pointed at another installation's file.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

BASENAME = "secrets.env"


def resolve(*, environ: Mapping[str, str], repo_root: Path) -> Path:
    """The file a process with this environment and checkout reads or writes."""
    override = environ.get("SECRETS_PATH")
    if override:
        return Path(override).expanduser()
    return repo_root / BASENAME
