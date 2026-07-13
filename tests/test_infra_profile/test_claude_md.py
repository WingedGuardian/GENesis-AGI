"""CLAUDE.md block writer: gate, placeholder preservation, idempotence."""

from __future__ import annotations

from unittest.mock import patch

from genesis.infra_profile.claude_md import update_block

_SEEDED = """# My install

<!-- begin:container-specs -->
## Container
- **Specs**: (populated by Genesis on first boot)
<!-- end:container-specs -->

<!-- begin:network-identity -->
## Network Identity
- **Container IP**: 10.0.0.2
<!-- end:network-identity -->
"""


def _profile():
    return {
        "planes": {"host": {"available": False}},
        "sections": {
            "memory": {
                "status": "ok",
                "hash": "h",
                "facts": {"cgroup_memory_max": 17179869184},
                "metrics": {},
            },
        },
    }


def test_rewrites_only_its_block(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(_SEEDED)
    assert update_block(_profile(), claude_md_path=md) is True
    text = md.read_text()
    assert "16.0 GiB" in text
    assert "(populated by Genesis on first boot)" not in text
    # the neighbouring block is untouched
    assert "- **Container IP**: 10.0.0.2" in text
    assert text.count("<!-- begin:container-specs -->") == 1


def test_empty_profile_leaves_placeholder(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(_SEEDED)
    assert update_block({}, claude_md_path=md) is False
    assert "(populated by Genesis on first boot)" in md.read_text()


def test_byte_identical_rewrite_skipped(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(_SEEDED)
    assert update_block(_profile(), claude_md_path=md) is True
    assert update_block(_profile(), claude_md_path=md) is False  # second run: no change


def test_update_in_progress_gate(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(_SEEDED)
    with patch("genesis.env.update_in_progress", return_value=True):
        assert update_block(_profile(), claude_md_path=md) is False
    assert "(populated by Genesis on first boot)" in md.read_text()


def test_gate_bypass_for_updater_cli(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(_SEEDED)
    with patch("genesis.env.update_in_progress", return_value=True):
        assert update_block(_profile(), claude_md_path=md, ignore_update_gate=True) is True


def test_missing_file_not_seeded(tmp_path):
    assert update_block(_profile(), claude_md_path=tmp_path / "CLAUDE.md") is False
    assert not (tmp_path / "CLAUDE.md").exists()
