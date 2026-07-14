"""Edge coverage for the qdrant and host collectors (PR #1019 review W2/W3)."""

from __future__ import annotations

from unittest.mock import patch

from genesis.infra_profile.collectors.host import HOST_SECTIONS, collect_host
from genesis.infra_profile.collectors.qdrant_facts import collect_qdrant
from genesis.infra_profile.types import PLANE_HOST, STATUS_ERROR


async def test_qdrant_unreachable_degrades():
    result = await collect_qdrant(base_url="http://127.0.0.1:1")  # nothing listens
    assert result.status == STATUS_ERROR
    assert "unreachable" in result.error


async def test_qdrant_malformed_json_degrades():
    with patch(
        "genesis.infra_profile.collectors.qdrant_facts._get_json",
        side_effect=ValueError("not json"),
    ):
        result = await collect_qdrant(base_url="http://localhost:6333")
    assert result.status == STATUS_ERROR


async def test_host_plane_without_guardian():
    available, reason, sections = await collect_host(None)
    assert available is False
    assert "no guardian" in reason
    assert [s.name for s in sections] == list(HOST_SECTIONS)
    assert all(s.status == "unavailable" and s.plane == PLANE_HOST for s in sections)


async def test_host_plane_with_incompatible_remote_degrades():
    """A remote without host_profile() (pre-PR2 GuardianRemote build) degrades
    to plane-unavailable — never raises into the refresh."""
    available, reason, sections = await collect_host(object())
    assert available is False
    assert "host-profile call failed" in reason
    assert len(sections) == len(HOST_SECTIONS)
