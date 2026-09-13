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
from packaging.version import Version

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


def _qdrant_compatible(v: Version, s_major: int, s_minor: int) -> bool:
    """Qdrant's own client/server rule, from qdrant_client/common/version_check.py:
    same major, and abs(server.minor - client.minor) <= 1."""
    return v.major == s_major and abs(v.minor - s_minor) <= 1


def _candidate_versions(s_major: int, s_minor: int) -> list[Version]:
    """A dense grid around the server version, spanning both boundaries.

    Deliberately a GRID rather than a handful of sample points. An earlier version
    of this test probed three specific strings ("{major}.{minor+2}.0", one
    lower-side value, "{major+1}.0.0") and every one of its gaps was a real hole:

      - probing only ``.0`` of a disallowed minor let ``!=1.16.0`` through while
        ``1.16.1`` still resolved, so patch levels are enumerated;
      - probing ``minor - 2`` computed a valid minor when the server minor was 0
        or 1, so the window is now computed per candidate rather than assumed;
      - probing only ``major + 1`` let ``>=0`` admit 0.x clients, so majors below
        the server's are covered too.

    The grid is the declared coverage model, and it is a model — a version outside
    it has no cell and so passes in silence. It spans two majors either side and
    three minors either side of the server, which is far wider than any plausible
    coordinated bump.
    """
    majors = range(max(0, s_major - 2), s_major + 3)
    minors = range(max(0, s_minor - 3), s_minor + 4)
    patches = (0, 1, 7)  # .0 is not representative of a minor line
    return [Version(f"{a}.{b}.{c}") for a in majors for b in minors for c in patches]


def test_qdrant_client_pin_admits_only_compatible_clients():
    """Every version the specifier admits must satisfy Qdrant's compatibility rule.

    This is the coupling the qdrant-client pin exists to hold. Without it the pin
    is only a comment in pyproject.toml, and moving EITHER number — the client
    specifier or the install.sh server default — leaves CI green while the pairing
    goes unsupported.
    """
    req = _qdrant_client_requirement()
    s_major, s_minor = _qdrant_server_version()

    admitted_incompatible: list[str] = []
    admitted_compatible: list[str] = []
    for v in _candidate_versions(s_major, s_minor):
        if not req.specifier.contains(str(v)):
            continue
        target = (
            admitted_compatible
            if _qdrant_compatible(v, s_major, s_minor)
            else admitted_incompatible
        )
        target.append(str(v))

    assert not admitted_incompatible, (
        f"'{req}' admits {admitted_incompatible} — incompatible with server "
        f"{s_major}.{s_minor}.x, whose rule is same major and at most one minor "
        f"apart. Bump BOTH the client specifier and QDRANT_VERSION, or neither."
    )
    assert admitted_compatible, (
        f"'{req}' admits NO client compatible with server {s_major}.{s_minor}.x — "
        f"the pin and the server default have drifted apart in the other direction."
    )


def test_qdrant_client_pin_is_bounded_at_all():
    """An unbounded specifier is what created the skew this pin fixes.

    ``qdrant-client`` carried no specifier at all, so a fresh install resolved to
    whatever was newest and paired it with a server pinned three releases back.
    Guard the SHAPE, not just the current numbers: a future edit that drops the
    upper bound reintroduces the drift even if it happens to resolve correctly on
    the day it lands.
    """
    req = _qdrant_client_requirement()
    operators = {spec.operator for spec in req.specifier}
    assert operators & {"<", "<=", "==", "~="}, (
        f"'{req}' has no upper bound — the client will drift past the server "
        "again. Qdrant requires the two stay within one minor."
    )
