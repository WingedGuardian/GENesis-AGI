"""Fixtures shared by more than one test module in this directory.

The `systemctl` fence this file used to carry (issue #2863) is now the
suite-wide live fence in `tests/_live_fence.py`, armed from `tests/conftest.py`:
the session-bus variables point nowhere, `systemctl` and friends reach a stub,
and the `user_manager` marker keeps the real bus for a test that needs it.
"""

import os
import pwd
from pathlib import Path

import pytest

from tests.test_scripts._deploy_candidates_world import (  # noqa: F401  (deploy_candidates)
    dc,
    dc_ready,
    dc_world,
)
from tests.test_scripts._deploy_station import station  # noqa: F401  (the deploy script's fixture)
from tests.test_scripts._live_repair_world import mod, w  # noqa: F401  (live-move repair)


def _real_kit_listing() -> list[tuple[str, str, int, int]]:
    """The real ~/.genesis/lib, entry by entry (name, link target, size, mtime)."""
    root = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".genesis" / "lib"
    if not root.exists() and not root.is_symlink():
        return []
    found = []
    for top, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            p = os.path.join(top, name)
            st = os.lstat(p)
            link = os.readlink(p) if os.path.islink(p) else ""
            found.append((os.path.relpath(p, root), link, st.st_size, st.st_mtime_ns))
    return sorted(found)


@pytest.fixture(autouse=True, scope="session")
def _real_repair_kit_untouched():
    """K13 (#3492 D2): the kit installer takes <home> as a required argument and
    every test passes a scratch one, so the real install's boot repair kit is
    never written by a test run."""
    before = _real_kit_listing()
    yield
    assert _real_kit_listing() == before, "a test changed the real ~/.genesis/lib"
