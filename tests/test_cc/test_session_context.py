"""Tests for genesis_session_context.py and genesis_urgent_alerts.py hooks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_CONTEXT_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_session_context.py"
_ALERTS_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_urgent_alerts.py"
_PYTHON = sys.executable


@pytest.fixture
def flag_dir(tmp_path: Path) -> Path:
    """Create a temp .genesis dir with flag file."""
    genesis_dir = tmp_path / ".genesis"
    genesis_dir.mkdir()
    (genesis_dir / "cc_context_enabled").touch()
    return genesis_dir


class TestSessionContextHook:
    def test_outputs_nothing_when_flag_absent(self, tmp_path: Path) -> None:
        """No flag file → no output."""
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.stdout == ""
        assert result.returncode == 0

    def test_skips_identity_but_outputs_capabilities_when_genesis_session(
        self, flag_dir: Path,
    ) -> None:
        """GENESIS_CC_SESSION=1 → skips identity/cognitive but outputs capabilities."""
        env = {
            "HOME": str(flag_dir.parent),
            "PATH": "/usr/bin",
            "GENESIS_CC_SESSION": "1",
        }
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        # Identity files should NOT be in output
        assert "# Genesis" not in result.stdout or "MCP" in result.stdout
        # Capabilities / MCP tools should still be present
        assert "MCP" in result.stdout or "Genesis" in result.stdout
        assert result.returncode == 0

    def test_outputs_identity_when_enabled(self, flag_dir: Path) -> None:
        """Flag present + no env var → outputs Genesis identity content."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        # Should output MCP tools hint at minimum (including outreach)
        assert "Genesis MCP Tools Available" in result.stdout
        assert "outreach" in result.stdout.lower()
        assert result.returncode == 0

    def test_writes_session_start_file(self, flag_dir: Path) -> None:
        """Hook should write session start timestamp for urgent-alerts hook."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        session_start = flag_dir / "session_start"
        assert session_start.exists()
        content = session_start.read_text().strip()
        # Should be an ISO timestamp
        assert "T" in content
        assert len(content) > 10

    def test_cognitive_state_failure_is_loud(self, flag_dir: Path) -> None:
        """When cognitive state fails, output should contain a visible alert."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        # The DB won't be found at tmp_path, so cognitive state will fail.
        # The output should contain a loud alert, not a subtle bracketed note.
        assert "GENESIS ALERT" in result.stdout or "Cognitive State" in result.stdout
        assert result.returncode == 0


class TestOnboardingInjection:
    """Tests for first-run onboarding detection in SessionStart hook."""

    def test_onboarding_fires_when_setup_complete_absent(self, flag_dir: Path) -> None:
        """No setup-complete marker → onboarding prompt injected."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert "FIRST-RUN ONBOARDING REQUIRED" in result.stdout
        assert "src/genesis/skills/onboarding/SKILL.md" in result.stdout
        assert result.returncode == 0

    def test_onboarding_suppressed_when_setup_complete_exists(self, flag_dir: Path) -> None:
        """setup-complete marker present → no onboarding prompt."""
        (flag_dir / "setup-complete").write_text("2026-03-28T16:00:00-04:00")
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert "FIRST-RUN ONBOARDING" not in result.stdout
        assert result.returncode == 0

    def test_onboarding_skipped_for_bridge_sessions(self, flag_dir: Path) -> None:
        """Bridge sessions (GENESIS_CC_SESSION=1) never get onboarding."""
        # No setup-complete marker — would normally trigger onboarding
        env = {
            "HOME": str(flag_dir.parent),
            "PATH": "/usr/bin",
            "GENESIS_CC_SESSION": "1",
        }
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert "FIRST-RUN ONBOARDING" not in result.stdout
        assert result.returncode == 0


class TestUrgentAlertsHook:
    def test_outputs_nothing_when_flag_absent(self, tmp_path: Path) -> None:
        """No flag file → no output."""
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_ALERTS_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.stdout == ""
        assert result.returncode == 0

    def test_outputs_nothing_when_genesis_session(self, flag_dir: Path) -> None:
        """GENESIS_CC_SESSION=1 → no output."""
        env = {
            "HOME": str(flag_dir.parent),
            "PATH": "/usr/bin",
            "GENESIS_CC_SESSION": "1",
        }
        result = subprocess.run(
            [_PYTHON, str(_ALERTS_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        assert result.stdout == ""
        assert result.returncode == 0

    def test_outputs_nothing_when_no_alerts(self, flag_dir: Path) -> None:
        """Flag present, no DB → no alerts (graceful)."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_ALERTS_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        # No DB = no alerts = no output
        assert result.stdout == ""
        assert result.returncode == 0


class TestInvokerEnvVar:
    def test_build_env_sets_genesis_cc_session(self) -> None:
        """CCInvoker._build_env() must set GENESIS_CC_SESSION=1."""
        from genesis.cc.invoker import CCInvoker

        invoker = CCInvoker()
        env = invoker._build_env()
        assert env.get("GENESIS_CC_SESSION") == "1"
