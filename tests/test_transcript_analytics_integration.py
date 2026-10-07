"""Opt-in and resource admission contracts, without touching systemd."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from genesis.transcript_analytics import config, resources


@pytest.fixture
def base(tmp_path, monkeypatch):
    from genesis import _config_overlay

    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path)
    monkeypatch.delenv("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED", raising=False)
    monkeypatch.delenv(resources._CHILD, raising=False)
    path = tmp_path / "transcript_analytics.yaml"
    path.write_text("enabled: false\n")
    return path


def test_private_opt_in_and_kill(base, monkeypatch):
    assert not config.load(base).enabled
    base.with_suffix(".local.yaml").write_text("enabled: true\nram_bytes: 3221225472\n")
    cfg = config.load(base)
    assert cfg.enabled and cfg.ram_bytes == 3 * 1024**3
    assert cfg.ram_pct == cfg.cpu_pct == 25
    monkeypatch.setenv("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED", "1")
    assert not config.load(base).enabled


@pytest.mark.parametrize(
    "setting",
    [
        "enabled: yes-please",
        "ram_pct: 0",
        "cpu_pct: .nan",
        "scope: main",
        "ram_bytes: true",
        "data_dir: relative",
    ],
)
def test_bad_config_cannot_enable(base, setting):
    base.with_suffix(".local.yaml").write_text("enabled: true\n" + setting + "\n")
    with pytest.raises((ValueError, TypeError)):
        config.load(base)


def test_broken_private_overlay_does_not_fall_back(base):
    base.write_text("enabled: true\n")
    base.with_suffix(".local.yaml").write_text("[not a mapping]")
    with pytest.raises(ValueError):
        config.load(base)


@pytest.fixture
def admitted(monkeypatch):
    from genesis.hostmetrics import __main__ as metrics
    from genesis.hostmetrics.host import HostMemory
    from genesis.hostmetrics.preflight import Snapshot
    from genesis.hostmetrics.readings import Memory

    snap = Snapshot(
        Memory(1024**3, 1024**3, "cgroup"),
        HostMemory(unavailable="test"),
        4,
        0,
        {"cpu": 0, "memory": 0, "io": 0},
        {},
    )
    monkeypatch.setattr(metrics, "take_snapshot", lambda *args: snap)
    monkeypatch.setattr(resources.shutil, "which", lambda _: "/usr/bin/systemd-run")
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(resources.subprocess, "run", run)
    return run, snap, metrics


def test_scope_caps_command_and_exit(base, admitted):
    run, _, _ = admitted
    cfg = config.Config(enabled=True)
    assert resources.ensure_capped(["transcripts", "ingest"], cfg) == 0
    args = run.call_args.args[0]
    assert "MemoryMax=268435456" in args
    assert "MemorySwapMax=0" in args
    assert "CPUQuota=100.00%" in args
    assert args[-2:] == ["transcripts", "ingest"]
    assert run.call_args.kwargs["env"][resources._CHILD] == "268435456,100.00"


def test_admission_deferred_does_not_start(base, admitted, monkeypatch, capsys):
    run, snap, metrics = admitted
    monkeypatch.setattr(metrics, "take_snapshot", lambda *args: replace(snap, memory=None))
    assert resources.ensure_capped([], config.Config(enabled=True)) == 75
    assert "ASK" in capsys.readouterr().err
    run.assert_not_called()


def test_no_uncapped_fallback(base, admitted, monkeypatch):
    run, _, _ = admitted
    monkeypatch.setattr(resources.shutil, "which", lambda _: None)
    assert resources.ensure_capped([], config.Config(enabled=True)) == 69
    run.assert_not_called()


@pytest.mark.parametrize("ram_bytes,available,verdict", [(None, 100, "WAIT"), (2**31, 2**30, "NO")])
def test_preflight_refuses_current_load_or_oversized_request(
    base, admitted, monkeypatch, capsys, ram_bytes, available, verdict
):
    run, snap, metrics = admitted
    memory = replace(snap.memory, available=available)
    monkeypatch.setattr(metrics, "take_snapshot", lambda *args: replace(snap, memory=memory))
    assert resources.ensure_capped([], config.Config(enabled=True, ram_bytes=ram_bytes)) == 75
    assert verdict in capsys.readouterr().err
    run.assert_not_called()


def test_child_flag_requires_kernel_limits(base, monkeypatch):
    monkeypatch.setenv(resources._CHILD, "1024,25")
    monkeypatch.setattr(resources, "_enforced", lambda _: False)
    assert resources.ensure_capped([], config.Config(enabled=True)) == 69
    monkeypatch.setattr(resources, "_enforced", lambda _: True)
    assert resources.ensure_capped([], config.Config(enabled=True)) is None


@pytest.mark.parametrize(
    "memory,swap,cpu,expected",
    [
        ("1024", "0", "25000 100000", True),
        ("max", "0", "25000 100000", False),
        ("1024", "max", "25000 100000", False),
        ("1024", "0", "max 100000", False),
        ("2048", "0", "25000 100000", False),
        ("1024", "0", "50000 100000", False),
    ],
)
def test_kernel_limit_verification(monkeypatch, memory, swap, cpu, expected):
    files = {
        "cgroup": "0::/user.slice/job.scope\n",
        "memory.max": memory,
        "memory.swap.max": swap,
        "cpu.max": cpu,
    }
    monkeypatch.setattr(Path, "read_text", lambda path: files[path.name])
    assert resources._enforced("1024,25") is expected
