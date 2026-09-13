"""Locks on dependency pins that are COUPLED to something else in the repo.

A pin whose correctness depends on another file's value is a convention, and
conventions decay silently — nothing fails when the two drift apart. These tests
are the chokepoint: they re-derive the coupling from both sources and fail when
it breaks, so the next person to bump either number is told.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]


def _qdrant_client_requirement() -> Requirement:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    for dep in data["project"]["dependencies"]:
        if Requirement(dep).name == "qdrant-client":
            return Requirement(dep)
    pytest.fail("qdrant-client is not declared in pyproject.toml dependencies")


def _qdrant_server_version() -> tuple[int, int]:
    """The server version scripts/install.sh installs when Qdrant is absent."""
    text = (REPO_ROOT / "scripts" / "install.sh").read_text()
    m = re.search(r'QDRANT_VERSION="\$\{QDRANT_VERSION:-([0-9]+)\.([0-9]+)\.[0-9]+\}"', text)
    assert m, "could not find the QDRANT_VERSION default in scripts/install.sh"
    return int(m.group(1)), int(m.group(2))


def test_qdrant_client_pin_tracks_the_server_version():
    """The client specifier must admit ONLY clients the server supports.

    Qdrant's own compatibility rule (``qdrant_client/common/version_check.py``)
    is: same major, and ``abs(server.minor - client.minor) <= 1``. So a 1.14
    server admits a 1.13-1.15 client and nothing else.

    This is the coupling the qdrant-client pin exists to hold. Without this test
    it is only a comment in pyproject.toml, and moving EITHER number — the
    client specifier or the install.sh server default — leaves CI green while
    the pairing goes unsupported.
    """
    req = _qdrant_client_requirement()
    s_major, s_minor = _qdrant_server_version()

    admitted = [
        f"{s_major}.{minor}.0"
        for minor in range(max(0, s_minor - 1), s_minor + 2)
        if req.specifier.contains(f"{s_major}.{minor}.0")
    ]
    assert admitted, (
        f"'{req}' admits no client compatible with server {s_major}.{s_minor}.x "
        f"(the rule allows minors {max(0, s_minor - 1)}-{s_minor + 1})"
    )

    # Nothing OUTSIDE the supported window may be admitted, in either direction.
    for bad in (f"{s_major}.{s_minor + 2}.0", f"{s_major}.{max(0, s_minor - 2)}.0"):
        assert not req.specifier.contains(bad), (
            f"'{req}' admits client {bad}, which is more than one minor from "
            f"server {s_major}.{s_minor}.x — bump BOTH or neither"
        )

    # A different MAJOR is never compatible, whatever the minor.
    assert not req.specifier.contains(f"{s_major + 1}.0.0"), (
        f"'{req}' admits a client one major ahead of server {s_major}.{s_minor}.x"
    )


def test_qdrant_client_pin_is_bounded_at_all():
    """An unbounded specifier is what created the skew this pin fixes.

    ``qdrant-client`` carried no specifier at all, so a fresh install resolved to
    whatever was newest and paired it with a server pinned three releases back.
    Guard the SHAPE, not just the current numbers: a future edit that drops the
    upper bound reintroduces the drift even if it happens to resolve correctly
    on the day it lands.
    """
    req = _qdrant_client_requirement()
    operators = {spec.operator for spec in req.specifier}
    assert operators & {"<", "<=", "==", "~="}, (
        f"'{req}' has no upper bound — the client will drift past the server "
        "again. Qdrant requires the two stay within one minor."
    )
