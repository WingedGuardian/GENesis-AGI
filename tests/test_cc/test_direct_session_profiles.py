"""Tests for direct session profile definitions and tool restrictions.

Validates that profile changes don't accidentally grant or revoke tool access.
"""

from genesis.cc.direct_session import (
    PROFILES,
    VALID_PROFILES,
)

# --- Profile existence ---

def test_valid_profiles_matches_profiles_dict():
    """VALID_PROFILES frozenset must match PROFILES keys."""
    assert frozenset(PROFILES.keys()) == VALID_PROFILES


def test_all_expected_profiles_exist():
    assert "observe" in PROFILES
    assert "interact" in PROFILES
    assert "research" in PROFILES


# --- Universal safety blocks (all profiles) ---

_UNIVERSAL_BLOCKED = {
    "Bash", "Edit", "Write", "NotebookEdit",
    "mcp__genesis-health__task_submit",
    "mcp__genesis-health__settings_update",
    "mcp__genesis-health__direct_session_run",
    "mcp__genesis-health__module_call",
}


def test_observe_blocks_universal():
    for tool in _UNIVERSAL_BLOCKED:
        assert tool in PROFILES["observe"], f"observe should block {tool}"


def test_interact_blocks_universal():
    for tool in _UNIVERSAL_BLOCKED:
        assert tool in PROFILES["interact"], f"interact should block {tool}"


def test_research_blocks_universal():
    for tool in _UNIVERSAL_BLOCKED:
        assert tool in PROFILES["research"], f"research should block {tool}"


# --- Observe: most restrictive ---

def test_observe_blocks_browser_interaction():
    assert "mcp__genesis-health__browser_click" in PROFILES["observe"]
    assert "mcp__genesis-health__browser_fill" in PROFILES["observe"]
    assert "mcp__genesis-health__browser_run_js" in PROFILES["observe"]


def test_observe_blocks_memory_writes():
    assert "mcp__genesis-memory__memory_store" in PROFILES["observe"]
    assert "mcp__genesis-memory__observation_write" in PROFILES["observe"]
    assert "mcp__genesis-memory__procedure_store" in PROFILES["observe"]


def test_observe_blocks_outreach_send():
    assert "mcp__genesis-outreach__outreach_send" in PROFILES["observe"]
    assert "mcp__genesis-outreach__outreach_send_and_wait" in PROFILES["observe"]


def test_observe_blocks_follow_ups():
    assert "mcp__genesis-health__follow_up_create" in PROFILES["observe"]


# --- Research: memory writes + follow-ups, no browser interaction ---

def test_research_allows_memory_writes():
    assert "mcp__genesis-memory__memory_store" not in PROFILES["research"]
    assert "mcp__genesis-memory__observation_write" not in PROFILES["research"]
    assert "mcp__genesis-memory__procedure_store" not in PROFILES["research"]


def test_research_allows_follow_ups():
    assert "mcp__genesis-health__follow_up_create" not in PROFILES["research"]


def test_research_blocks_browser_interaction():
    assert "mcp__genesis-health__browser_click" in PROFILES["research"]
    assert "mcp__genesis-health__browser_fill" in PROFILES["research"]
    assert "mcp__genesis-health__browser_run_js" in PROFILES["research"]


def test_research_blocks_outreach_send():
    assert "mcp__genesis-outreach__outreach_send" in PROFILES["research"]
    assert "mcp__genesis-outreach__outreach_send_and_wait" in PROFILES["research"]


# --- Interact: most permissive — browser + memory + outreach ---

def test_interact_allows_browser_interaction():
    assert "mcp__genesis-health__browser_click" not in PROFILES["interact"]
    assert "mcp__genesis-health__browser_fill" not in PROFILES["interact"]
    assert "mcp__genesis-health__browser_run_js" not in PROFILES["interact"]


def test_interact_allows_memory_writes():
    assert "mcp__genesis-memory__memory_store" not in PROFILES["interact"]
    assert "mcp__genesis-memory__observation_write" not in PROFILES["interact"]
    assert "mcp__genesis-memory__procedure_store" not in PROFILES["interact"]


def test_interact_allows_outreach_send():
    assert "mcp__genesis-outreach__outreach_send" not in PROFILES["interact"]
    assert "mcp__genesis-outreach__outreach_send_and_wait" not in PROFILES["interact"]


def test_interact_allows_follow_ups():
    assert "mcp__genesis-health__follow_up_create" not in PROFILES["interact"]


def test_interact_blocks_outreach_engagement():
    """Interact should not allow modifying outreach preferences/engagement."""
    assert "mcp__genesis-outreach__outreach_engagement" in PROFILES["interact"]
    assert "mcp__genesis-outreach__outreach_preferences" in PROFILES["interact"]


def test_interact_blocks_recon_writes():
    assert "mcp__genesis-recon__recon_store_finding" in PROFILES["interact"]


def test_interact_blocks_evolution_propose():
    """Publishing sessions should not propose evolution changes."""
    assert "mcp__genesis-memory__evolution_propose" in PROFILES["interact"]


# --- Profile shape assertions ---

def test_observe_is_most_restrictive():
    """Observe should block the most tools."""
    assert len(PROFILES["observe"]) >= len(PROFILES["research"])
    assert len(PROFILES["observe"]) >= len(PROFILES["interact"])


def test_interact_and_research_share_universal_base():
    """Both interact and research include all universal blocks."""
    interact_set = set(PROFILES["interact"])
    research_set = set(PROFILES["research"])
    assert _UNIVERSAL_BLOCKED.issubset(interact_set)
    assert _UNIVERSAL_BLOCKED.issubset(research_set)
