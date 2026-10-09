"""Opt-in and resource admission contracts, without touching systemd."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from genesis.transcript_analytics import config, resources


@pytest.fixture
def base(tmp_path, monkeypatch):
    from genesis import _config_overlay

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis"))

    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path)
    monkeypatch.delenv("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED", raising=False)
    monkeypatch.delenv(resources._CHILD, raising=False)
    path = tmp_path / "transcript_analytics.yaml"
    path.write_text("enabled: false\n")
    monkeypatch.setattr(config, "_base_path", lambda: path)
    return path


def test_private_opt_in_and_kill(base, monkeypatch):
    assert not config.load(base).enabled
    base.with_suffix(".local.yaml").write_text("enabled: true\nram_bytes: 3221225472\n")
    cfg = config.load(base)
    assert cfg.enabled and cfg.ram_bytes == 3 * 1024**3
    assert cfg.ram_pct == cfg.cpu_pct == 25
    monkeypatch.setenv("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED", "1")
    assert not config.load(base).enabled


def test_kill_preserves_validated_saved_paths_and_settings(base, monkeypatch):
    projects, data = base.parent / "chosen-projects", base.parent / "chosen-data"
    base.with_suffix(".local.yaml").write_text(
        f"enabled: true\nprojects_dir: {projects}\ndata_dir: {data}\nevidence_records: 9\n"
    )
    expected = config.load(base)
    monkeypatch.setenv("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED", "1")
    assert config.load(base) == replace(expected, enabled=False)
    assert config.load(base, ignore_kill=True) == expected


@pytest.mark.parametrize("state", ["missing", "invalid-base", "invalid-overlay"])
def test_kill_does_not_hide_missing_or_invalid_install(base, monkeypatch, state):
    if state == "missing":
        base.unlink()
    elif state == "invalid-base":
        base.write_text("[not a mapping]\n")
    else:
        base.with_suffix(".local.yaml").write_text("[not a mapping]\n")
    monkeypatch.setenv("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED", "1")
    with pytest.raises(ValueError):
        config.load(base)


@pytest.mark.parametrize("value,valid", [(None, True), (True, False), (1, False),
                                       (2**24 - 1, False), (2**24, True), (2**24 + 1, True)])
def test_explicit_ram_respects_shared_startup_floor(value, valid):
    from genesis.hostmetrics.run import MIN_RAM

    assert MIN_RAM == 2**24
    if valid:
        assert config.from_values({"ram_bytes": value}).ram_bytes == value
    else:
        with pytest.raises(ValueError):
            config.from_values({"ram_bytes": value})


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
def admitted(base, monkeypatch):
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
    from genesis.hostmetrics import run as runner

    base.write_text("enabled: true\n")
    run = Mock(return_value=Mock(wait=Mock(return_value=0)))
    owned = Mock(spec=runner.OwnedProcess)
    owned.signals = 0
    owned.__enter__ = Mock(return_value=owned)
    owned.__exit__ = Mock(return_value=False)
    owned.start_scoped.side_effect = lambda argv, unit, **kwargs: run(argv, **kwargs)
    owned.wait.side_effect = lambda: run.return_value.wait()
    owned.outcome.side_effect = lambda code: code
    run.owned = owned
    monkeypatch.setattr(runner, "OwnedProcess", lambda: owned)
    monkeypatch.setattr(resources, "_await_ready", lambda *args: True)
    return run, snap, metrics


def test_scope_caps_command_and_exit(base, admitted):
    run, _, _ = admitted
    cfg = config.Config(enabled=True)
    assert resources.ensure_capped(["transcripts", "ingest"], cfg) == 0
    args = run.call_args.args[0]
    assert "MemoryMax=268435456" in args
    assert "MemorySwapMax=0" in args
    assert "CPUQuota=100.00%" in args
    assert "RuntimeMaxSec=1h" in args
    assert any(value.startswith("--unit=genesis-job-transcript-analytics-") for value in args)
    assert args[-2:] == ["transcripts", "ingest"]
    assert run.call_args.kwargs["env"][resources._CHILD] == "268435456,100.00"


@pytest.mark.parametrize("percent,valid", [(1.0, False), (1.5625, True)])
def test_computed_ram_floor_refuses_launch_without_inflating_cap(base, admitted, percent, valid):
    from genesis.hostmetrics.run import MIN_RAM

    launch, _, _ = admitted
    base.with_suffix(".local.yaml").write_text(f"ram_pct: {percent}\n")
    result = resources.ensure_capped(["transcripts", "ingest"], config.load(base))
    if valid:
        assert result == 0
        assert f"MemoryMax={MIN_RAM}" in launch.call_args.args[0]
    else:
        assert result == 69
        launch.assert_not_called()


def test_admission_deferred_does_not_start(base, admitted, monkeypatch, capsys):
    run, snap, metrics = admitted
    monkeypatch.setattr(metrics, "take_snapshot", lambda *args: replace(snap, memory=None))
    assert resources.ensure_capped([], config.Config(enabled=True)) == 75
    assert "ASK" in capsys.readouterr().err
    run.assert_not_called()


def test_no_uncapped_fallback(base, admitted, monkeypatch):
    run, _, _ = admitted
    run.side_effect = FileNotFoundError("systemd-run missing")
    assert resources.ensure_capped([], config.Config(enabled=True)) == 69
    assert run.call_count == 1


@pytest.mark.parametrize("ram_bytes,available,verdict", [(None, 100, "WAIT"), (2**31, 2**30, "NO")])
def test_preflight_refuses_current_load_or_oversized_request(
    base, admitted, monkeypatch, capsys, ram_bytes, available, verdict
):
    run, snap, metrics = admitted
    memory = replace(snap.memory, available=available)
    if ram_bytes is not None:
        base.with_suffix(".local.yaml").write_text(f"ram_bytes: {ram_bytes}\n")
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


def test_admission_is_exclusive_until_positive_worker_confirmation(base, admitted):
    import fcntl

    from genesis.env import genesis_home

    run, _, _ = admitted
    path = genesis_home() / "locks/transcript-analytics-admission.lock"
    path.parent.mkdir(parents=True)
    with path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert resources.ensure_capped([], config.Config(enabled=True)) == 75
    run.assert_not_called()


def test_launch_rejection_is_unavailable_and_child_failure_preserved(base, admitted, monkeypatch):
    run, _, _ = admitted
    proc = run.return_value
    proc.poll.return_value = 1
    proc.wait.return_value = 1
    monkeypatch.setattr(resources, "_await_ready", lambda *args: False)
    assert resources.ensure_capped([], config.Config(enabled=True)) == 69
    assert run.owned.__exit__.call_count == 1
    assert run.owned.__exit__.call_args.args[0].__name__ == "ProbeRefused"
    monkeypatch.setattr(resources, "_await_ready", lambda *args: True)
    assert resources.ensure_capped([], config.Config(enabled=True)) == 1


def test_persistent_disable_overrides_stale_enabled_config(base, admitted):
    run, _, _ = admitted
    base.write_text("enabled: false\n")
    assert resources.ensure_capped([], config.Config(enabled=True)) == 69
    run.assert_not_called()


def test_positive_acknowledgement_requires_real_enforcement(monkeypatch):
    import os

    for enforced, wanted in [(True, b"1"), (False, b"0")]:
        read, write = os.pipe()
        monkeypatch.setenv(resources._READY, str(write))
        resources._acknowledge(enforced)
        assert os.read(read, 1) == wanted
        os.close(read)
        assert resources._READY not in resources.os.environ


@pytest.mark.parametrize("marker", ["nan,25", "1024,inf", "0,25", "1024,0"])
def test_nonfinite_or_zero_caps_are_unavailable(marker):
    assert not resources._enforced(marker)


def test_evidence_config_matches_runtime_zero_context_and_minimum_budget(base):
    base.with_suffix(".local.yaml").write_text(
        "enabled: true\nevidence_records: 0\nevidence_bytes: 1024\n"
    )
    assert config.load(base).evidence_records == 0
    base.with_suffix(".local.yaml").write_text("enabled: true\nevidence_bytes: 1023\n")
    with pytest.raises(ValueError):
        config.load(base)


def test_unreachable_manager_ledger_is_unavailable(base, admitted, monkeypatch):
    run, snap, metrics = admitted
    monkeypatch.setattr(
        metrics, "take_snapshot", lambda *args: replace(snap, reserved_beyond_use=None)
    )
    assert resources.ensure_capped([], config.Config(enabled=True)) == 69
    run.assert_not_called()


def test_child_acknowledges_before_cli_import_or_failure(monkeypatch):
    import runpy

    events = []
    monkeypatch.setattr(resources, "_enforced", lambda marker: True)
    monkeypatch.setattr(resources, "_acknowledge", lambda enforced: events.append("ack"))

    def fail(*args, **kwargs):
        events.append("cli")
        raise SystemExit(2)

    monkeypatch.setattr(runpy, "run_module", fail)
    with pytest.raises(SystemExit) as exc:
        resources._child_main()
    assert exc.value.code == 2 and events == ["ack", "cli"]
