"""Tests for genesis_session_context.py and genesis_urgent_alerts.py hooks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_CONTEXT_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_session_context.py"
)
_ALERTS_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_urgent_alerts.py"
)
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
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.stdout == ""
        assert result.returncode == 0

    def test_skips_identity_but_outputs_capabilities_when_genesis_session(
        self,
        flag_dir: Path,
    ) -> None:
        """GENESIS_CC_SESSION=1 → skips identity/cognitive but outputs capabilities."""
        env = {
            "HOME": str(flag_dir.parent),
            "PATH": "/usr/bin",
            "GENESIS_CC_SESSION": "1",
        }
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
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
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        # Should output MCP tools hint at minimum (including outreach)
        assert "Genesis MCP Tools Available" in result.stdout
        assert "outreach" in result.stdout.lower()
        assert result.returncode == 0

    def test_session_config_block_carries_header_directive(self, flag_dir: Path) -> None:
        """The top Session Configuration block must instruct the first-reply header.

        Regression guard: the `[<model> / <effort>]` header is specified in
        CONVERSATION.md but fired unreliably when only that deep spec existed.
        The hook now echoes the directive into the high-salience top block — if
        that echo is dropped, the header silently stops appearing again.
        """
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert "## Session Configuration" in result.stdout
        assert "status header" in result.stdout
        # Effort defaults to "high" with no session_config.json present.
        assert "[<model> / high]" in result.stdout
        assert result.returncode == 0

    def test_writes_session_start_file(self, flag_dir: Path) -> None:
        """Hook should write session start timestamp for urgent-alerts hook."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        session_start = flag_dir / "session_start"
        assert session_start.exists()
        content = session_start.read_text().strip()
        # Should be an ISO timestamp
        assert "T" in content
        assert len(content) > 10

    def test_missing_essential_knowledge_is_silent(self, flag_dir: Path) -> None:
        """When essential knowledge file is missing, hook succeeds silently.

        Foreground sessions use the essential_knowledge path and degrade
        silently by design — missing EK is advisory, not an error.
        """
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        # Essential knowledge is advisory — missing file is not an error.
        # The hook should still succeed and output capabilities.
        assert result.returncode == 0

    def test_cognitive_state_failure_is_loud(self, flag_dir: Path) -> None:
        """When cognitive state fails in a genesis session, output a visible alert.

        The loud-alert path only runs for background/ego sessions
        (GENESIS_CC_SESSION=1); foreground sessions use the essential_knowledge
        path instead and degrade silently by design.
        """
        env = {
            "HOME": str(flag_dir.parent),
            "PATH": "/usr/bin",
            "GENESIS_CC_SESSION": "1",
        }
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        # Background sessions should still succeed but may emit alerts.
        assert result.returncode == 0


def _load_context_module():
    """Import genesis_session_context.py as a module for direct unit tests.

    The script has no import-time side effects (main() is under __main__), so a
    plain spec-load is safe and avoids a subprocess for the pure helpers.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_gsc", _CONTEXT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestModelDisplayName:
    """Pure mapper: CC model identifier → `Display Name Version` (or None)."""

    def test_maps_known_ids(self) -> None:
        m = _load_context_module()
        assert m._model_display_name("claude-opus-4-8") == "Opus 4.8"
        assert m._model_display_name("claude-sonnet-4-6") == "Sonnet 4.6"
        assert m._model_display_name("claude-haiku-4-5") == "Haiku 4.5"
        assert m._model_display_name("claude-fable-5") == "Fable 5"

    def test_strips_context_window_suffix(self) -> None:
        """`claude-opus-4-8[1m]` (1M-context variant) still maps."""
        m = _load_context_module()
        assert m._model_display_name("claude-opus-4-8[1m]") == "Opus 4.8"

    def test_strips_trailing_date_stamp(self) -> None:
        m = _load_context_module()
        assert m._model_display_name("claude-haiku-4-5-20251001") == "Haiku 4.5"

    def test_unknown_and_empty_return_none(self) -> None:
        """A model newer than the table degrades to None → caller injects raw id."""
        m = _load_context_module()
        assert m._model_display_name("claude-brandnew-9") is None
        assert m._model_display_name("") is None


class TestSessionConfigBlock:
    """The header directive builder — model-identity precedence + fallback."""

    def test_known_hook_model_yields_exact_header(self) -> None:
        m = _load_context_module()
        block = m._session_config_block("high", "claude-opus-4-8", "")
        assert "[Opus 4.8 / high]" in block
        assert "authoritative" in block
        assert "<model>" not in block  # fully resolved, no placeholder left

    def test_roster_model_wins_over_hook_model(self) -> None:
        """A routed peer (GENESIS_ROSTER_MODEL) takes precedence over CC's field."""
        m = _load_context_module()
        block = m._session_config_block("medium", "claude-opus-4-8", "GLM-4.6")
        assert "[GLM-4.6 / medium]" in block
        assert "Opus 4.8" not in block

    def test_unmapped_hook_model_injects_raw_id_with_instruction(self) -> None:
        m = _load_context_module()
        block = m._session_config_block("high", "claude-brandnew-9", "")
        assert "claude-brandnew-9" in block
        assert "Map it to its display name" in block
        assert "[<model> / high]" in block  # placeholder kept for the LLM to fill

    def test_absent_model_falls_back_to_env_derivation(self) -> None:
        m = _load_context_module()
        block = m._session_config_block("high", "", "")
        assert "[<model> / high]" in block
        assert "You are powered by" in block


class TestStatusHeaderStdinModel:
    """Integration: CC's SessionStart `model` field flows through to the header."""

    def test_hook_model_field_drives_header(self, flag_dir: Path) -> None:
        """A `model` in stdin JSON produces an exact, authoritative header."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            input='{"model": "claude-opus-4-8", "source": "compact"}',
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert "[Opus 4.8 / high]" in result.stdout
        assert result.returncode == 0

    def test_no_stdin_keeps_env_derivation_header(self, flag_dir: Path) -> None:
        """Backward compat: no stdin/model → legacy `[<model> / high]` directive."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert "[<model> / high]" in result.stdout
        assert "You are powered by" in result.stdout
        assert result.returncode == 0


class TestOnboardingInjection:
    """Tests for first-run onboarding detection in SessionStart hook."""

    def test_onboarding_fires_when_setup_complete_absent(self, flag_dir: Path) -> None:
        """No setup-complete marker → onboarding prompt injected."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_CONTEXT_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
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
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
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
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert "FIRST-RUN ONBOARDING" not in result.stdout
        assert result.returncode == 0


class TestUrgentAlertsHook:
    def test_outputs_nothing_when_flag_absent(self, tmp_path: Path) -> None:
        """No flag file → no output."""
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_ALERTS_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
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
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.stdout == ""
        assert result.returncode == 0

    def test_outputs_nothing_when_no_alerts(self, flag_dir: Path) -> None:
        """Flag present, no DB → no alerts (graceful)."""
        env = {"HOME": str(flag_dir.parent), "PATH": "/usr/bin"}
        result = subprocess.run(
            [_PYTHON, str(_ALERTS_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
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
