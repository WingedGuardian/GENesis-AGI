"""Tests for genesis.env deploy-state helpers.

Covers ``update_in_progress()`` — the shared signal the autonomy watchdog uses
to DEFER restarting genesis-server during a deploy (incident IR-2). The contract
is fail-open: any dead / absent / corrupt / expired signal → False, and the
helper must NEVER raise into its caller (the watchdog restart path).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.env import update_in_progress


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point genesis_home() at an isolated tmp dir (via the GENESIS_HOME env)."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    return tmp_path


def _write_state(
    home: Path,
    *,
    phase: str = "bootstrap",
    pid: int | None = None,
    started_at: str | None = None,
) -> None:
    """Write an update_state.json like update.sh::_write_state does."""
    if pid is None:
        pid = os.getpid()  # a real, alive, > 1 pid
    if started_at is None:
        started_at = datetime.now(UTC).isoformat()
    (home / "update_state.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "pid": pid,
                "started_at": started_at,
                "rollback_tag": "pre-update-x",
                "timestamp": started_at,
            }
        )
    )


def _kill_dead(_pid: int, _sig: int) -> None:
    """Stand-in for os.kill on a dead pid."""
    raise ProcessLookupError


class TestStateJsonPath:
    """The CLI path — update.sh writes update_state.json (the incident path)."""

    def test_false_when_nothing_present(self, home: Path):
        assert update_in_progress() is False

    def test_true_for_live_pid_mid_bootstrap(self, home: Path):
        _write_state(home, phase="bootstrap", pid=os.getpid())
        assert update_in_progress() is True

    def test_false_when_phase_done(self, home: Path):
        # "done" is written just before the file is removed — not in progress.
        _write_state(home, phase="done", pid=os.getpid())
        assert update_in_progress() is False

    def test_false_for_dead_pid(self, home: Path, monkeypatch: pytest.MonkeyPatch):
        _write_state(home, pid=os.getpid())
        monkeypatch.setattr(os, "kill", _kill_dead)
        assert update_in_progress() is False

    def test_false_for_pid_le_1(self, home: Path):
        # An AsyncMock().pid is 1 in py3.12 — must never count as a live deploy.
        _write_state(home, pid=1)
        assert update_in_progress() is False

    def test_false_for_stale_started_at(self, home: Path):
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        _write_state(home, pid=os.getpid(), started_at=old)
        assert update_in_progress() is False

    def test_true_when_started_at_missing(self, home: Path):
        # No timestamp → fall back to pid liveness alone.
        (home / "update_state.json").write_text(
            json.dumps({"phase": "bootstrap", "pid": os.getpid()})
        )
        assert update_in_progress() is True

    def test_true_for_naive_started_at(self, home: Path):
        # A naive (tz-less) timestamp is treated as UTC by the recency check,
        # so a fresh naive start still counts as in-progress (never wrongly stale).
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()  # no offset
        _write_state(home, pid=os.getpid(), started_at=naive)
        assert update_in_progress() is True

    def test_false_for_corrupt_json(self, home: Path):
        (home / "update_state.json").write_text("{not valid json")
        assert update_in_progress() is False


class TestPidFilePath:
    """The dashboard path — updates.py writes a bare-int update_in_progress.pid."""

    def test_true_for_live_pid_file(self, home: Path):
        (home / "update_in_progress.pid").write_text(str(os.getpid()))
        assert update_in_progress() is True

    def test_false_for_dead_pid_file(self, home: Path, monkeypatch: pytest.MonkeyPatch):
        (home / "update_in_progress.pid").write_text(str(os.getpid()))
        monkeypatch.setattr(os, "kill", _kill_dead)
        assert update_in_progress() is False

    def test_false_for_pid_le_1(self, home: Path):
        (home / "update_in_progress.pid").write_text("1")
        assert update_in_progress() is False

    def test_false_for_garbage_pid_file(self, home: Path):
        (home / "update_in_progress.pid").write_text("not-a-pid")
        assert update_in_progress() is False


def test_never_raises_fails_open_to_false(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unexpected error type must fail open to False, never propagate."""
    _write_state(home, pid=os.getpid())

    def _boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    # read_text isn't in any inner except tuple — only the outer catch saves us.
    monkeypatch.setattr(Path, "read_text", _boom)
    assert update_in_progress() is False


def test_pid_file_takes_precedence_but_either_signal_counts(home: Path):
    """Both signals present → still True (dashboard path checked first)."""
    (home / "update_in_progress.pid").write_text(str(os.getpid()))
    _write_state(home, phase="done", pid=1)  # JSON path alone would be False
    assert update_in_progress() is True


class TestUserTimezonePrecedence:
    """genesis.yaml ``timezone`` is authoritative; ``USER_TIMEZONE`` env is a
    DEPRECATED fallback consulted only when the file has no timezone key.

    ``_local_config`` reads ``Path.home()/.genesis/config/genesis.yaml`` (NOT
    ``GENESIS_HOME``), so we patch ``Path.home`` rather than reuse the ``home``
    fixture. Every case invalidates the module cache after mutating state.
    """

    @staticmethod
    def _write_cfg(home_dir: Path, *, timezone: str | None) -> None:
        cfg_dir = home_dir / ".genesis" / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        body = "github:\n  user: someone\n"
        if timezone is not None:
            body = f"timezone: {timezone}\n" + body
        (cfg_dir / "genesis.yaml").write_text(body)

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from genesis.env import _invalidate_local_config

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("USER_TIMEZONE", raising=False)
        _invalidate_local_config()
        yield
        _invalidate_local_config()

    def test_file_wins_over_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The flip: genesis.yaml outranks a DIFFERENT USER_TIMEZONE env value.
        from genesis.env import _invalidate_local_config, user_timezone

        self._write_cfg(tmp_path, timezone="Europe/Paris")
        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        _invalidate_local_config()
        assert user_timezone() == "Europe/Paris"

    def test_env_fallback_when_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # No genesis.yaml at all → env fallback preserves behavior (no regression).
        from genesis.env import _invalidate_local_config, user_timezone

        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        _invalidate_local_config()
        assert user_timezone() == "Asia/Tokyo"

    def test_env_fallback_when_file_has_no_timezone_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from genesis.env import _invalidate_local_config, user_timezone

        self._write_cfg(tmp_path, timezone=None)
        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        _invalidate_local_config()
        assert user_timezone() == "Asia/Tokyo"

    def test_utc_when_neither_present(self, tmp_path: Path):
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "UTC"

    def test_blank_file_value_falls_through_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A whitespace-only file timezone must not shadow the env fallback with "".
        self._write_cfg(tmp_path, timezone='"   "')
        monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "Asia/Tokyo"

    def test_blank_file_and_no_env_is_utc(self, tmp_path: Path):
        self._write_cfg(tmp_path, timezone='"   "')
        from genesis.env import _invalidate_local_config, user_timezone

        _invalidate_local_config()
        assert user_timezone() == "UTC"
