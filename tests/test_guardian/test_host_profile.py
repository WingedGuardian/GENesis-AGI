"""Tests for the host-side ``host-profile`` gather (guardian/host_profile.py)."""

from __future__ import annotations

import dataclasses

import pytest

from genesis.guardian import host_profile as hp
from genesis.guardian.config import GuardianConfig
from genesis.guardian.pool import StoragePoolStatus

# Real YAML shape from `incus config show genesis` on the live host
# (2026-07-12): limits.* at the top-level config map, PLUS device-nested
# limits.read/write that a naive scan would wrongly absorb.
_INCUS_CONFIG_YAML = """\
architecture: x86_64
config:
  boot.autostart: "true"
  limits.cpu: "8"
  limits.memory: 16GiB
  image.description: Ubuntu noble
devices:
  root:
    limits.read: 190MB
    limits.write: 90MB
    path: /
    pool: default
    type: disk
name: genesis
"""


class TestParseIncusLimits:
    def test_extracts_top_level_limits_only(self) -> None:
        limits = hp._parse_incus_limits(_INCUS_CONFIG_YAML)
        assert limits == {"limits.cpu": "8", "limits.memory": "16GiB"}

    def test_device_nested_limits_excluded(self) -> None:
        limits = hp._parse_incus_limits(_INCUS_CONFIG_YAML)
        assert "limits.read" not in limits
        assert "limits.write" not in limits

    def test_empty_input(self) -> None:
        assert hp._parse_incus_limits("") == {}
        assert hp._parse_incus_limits("name: genesis\n") == {}


class TestHostSystem:
    def test_shape_from_real_proc(self) -> None:
        """Runs against the real /proc — every field best-effort, no raise."""
        section = hp._host_system()
        assert section["nproc"] >= 1
        assert section["kernel_release"]
        assert section["hostname"]
        assert section["mem_total_kb"] > 0
        assert isinstance(section.get("loadavg", []), list)


class TestGatherHostProfile:
    @pytest.fixture
    def config(self) -> GuardianConfig:
        return GuardianConfig()

    async def test_three_sections_present_and_ok(self, config, monkeypatch) -> None:
        pool_status = StoragePoolStatus(
            detected=True, data_pct=61.19, metadata_pct=42.6,
            vg_free_bytes=34359738368, detail="lvm vg0 data=61.19 meta=42.6",
        )

        async def fake_measure(cfg):
            return pool_status

        monkeypatch.setattr("genesis.guardian.pool.measure_storage_pool", fake_measure)

        async def fake_run(*argv):
            if argv == ("incus", "version"):
                return "Client version: 6.0.0\nServer version: 6.0.0\n"
            if argv[:3] == ("incus", "config", "show"):
                return _INCUS_CONFIG_YAML
            if argv == ("systemd-detect-virt",):
                return "kvm\n"
            return None

        monkeypatch.setattr(hp, "_run", fake_run)

        result = await hp.gather_host_profile(config)
        assert result["ok"] is True
        assert set(result) >= {"host_system", "host_storage_pool", "host_virt"}
        pool = result["host_storage_pool"]
        assert pool["detected"] is True
        assert pool["data_pct"] == 61.19
        assert pool["tier"] in ("ok", "warn", "high", "crit", "unknown")
        virt = result["host_virt"]
        assert virt["incus_server_version"] == "6.0.0"
        assert virt["container_limits"] == {"limits.cpu": "8", "limits.memory": "16GiB"}
        assert virt["detect_virt"] == "kvm"
        assert virt["pve_version"] is None  # absent on the guardian VM

    async def test_section_failure_degrades_not_raises(self, config, monkeypatch) -> None:
        async def boom(cfg):
            raise RuntimeError("lvs exploded")

        monkeypatch.setattr("genesis.guardian.pool.measure_storage_pool", boom)

        async def fake_run(*argv):
            return None  # every external tool missing

        monkeypatch.setattr(hp, "_run", fake_run)

        result = await hp.gather_host_profile(config)
        assert result["ok"] is True  # partial data still ok — sections degrade
        assert "error" in result["host_storage_pool"]
        assert "lvs exploded" in result["host_storage_pool"]["error"]
        # host_virt with no tools: best-effort fields only, never a raise
        assert result["host_virt"]["pve_version"] is None
        assert result["host_system"]["nproc"] >= 1

    async def test_undetected_pool_reports_unknown_tier(self, config, monkeypatch) -> None:
        async def fake_measure(cfg):
            return StoragePoolStatus(detected=False, detail="incus unavailable")

        monkeypatch.setattr("genesis.guardian.pool.measure_storage_pool", fake_measure)
        section = await hp._host_storage_pool(config)
        assert section["detected"] is False
        assert section["tier"] == "unknown"

    async def test_all_sections_failed_flips_ok_false(self, config, monkeypatch) -> None:
        """Total host-side collapse must be distinguishable from a healthy
        plane: ok=False → exit 1 → the container degrades the plane instead
        of rendering three error rows (review 2026-07-13)."""

        async def boom(cfg):
            raise RuntimeError("pool boom")

        monkeypatch.setattr("genesis.guardian.pool.measure_storage_pool", boom)
        monkeypatch.setattr(hp, "_host_system", lambda: (_ for _ in ()).throw(OSError("no /proc")))

        async def virt_boom(cfg):
            raise RuntimeError("virt boom")

        monkeypatch.setattr(hp, "_host_virt", virt_boom)

        result = await hp.gather_host_profile(config)
        assert result["ok"] is False
        assert result["error"] == "all host sections failed"
        assert all(
            "error" in result[name]
            for name in ("host_system", "host_storage_pool", "host_virt")
        )

    def test_pool_status_fields_match_dataclass(self) -> None:
        """The gather emits StoragePoolStatus verbatim — if the dataclass gains
        or renames fields, the container-side facts/metrics split must be
        revisited (collectors/host.py allowlists)."""
        fields = {f.name for f in dataclasses.fields(StoragePoolStatus)}
        assert fields == {
            "detected", "data_pct", "metadata_pct",
            "vg_free_bytes", "pool_used_pct", "detail",
        }
