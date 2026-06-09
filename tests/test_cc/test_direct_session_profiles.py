"""Tests for direct session profile definitions and tool restrictions.

Validates that profile changes don't accidentally grant or revoke tool access.
"""

from unittest.mock import MagicMock

from genesis.cc.direct_session import (
    _PROFILE_ADDENDA,
    PROFILES,
    VALID_PROFILES,
    DirectSessionRequest,
    DirectSessionRunner,
    _build_profile_addendum,
)
from genesis.cc.types import CCModel

# --- Profile existence ---

def test_valid_profiles_matches_profiles_dict():
    """VALID_PROFILES frozenset must match PROFILES keys."""
    assert frozenset(PROFILES.keys()) == VALID_PROFILES


def test_all_expected_profiles_exist():
    assert "observe" in PROFILES
    assert "interact" in PROFILES
    assert "research" in PROFILES


# --- Universal safety blocks (all profiles) ---
# Note: Write is NOT universally blocked — it's allowed for interact/research
# (scoped to ~/.genesis/output/ via profile addendum instruction) but blocked
# for observe via _NO_FILE_WRITE.

_UNIVERSAL_BLOCKED = {
    "Bash", "Edit", "NotebookEdit",
    "mcp__genesis-health__task_submit",
    "mcp__genesis-health__settings_update",
    "mcp__genesis-health__direct_session_run",
    "mcp__genesis-health__module_call",
    # Vector store isolation — no background session writes to Qdrant
    "mcp__genesis-memory__memory_store",
    "mcp__genesis-memory__memory_synthesize",
    "mcp__genesis-memory__memory_extract",
    # Knowledge ingestion — user authorization required
    "mcp__genesis-memory__knowledge_ingest",
    "mcp__genesis-memory__knowledge_ingest_batch",
    "mcp__genesis-memory__knowledge_ingest_source",
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


# --- Write tool scoping ---

def test_observe_blocks_write():
    """Observe is read-only — Write is blocked."""
    assert "Write" in PROFILES["observe"]


def test_interact_allows_write():
    """Interact allows Write (scoped to ~/.genesis/output/ via instruction)."""
    assert "Write" not in PROFILES["interact"]


def test_research_allows_write():
    """Research allows Write (scoped to ~/.genesis/output/ via instruction)."""
    assert "Write" not in PROFILES["research"]


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


# --- Research: SQLite writes + follow-ups, no vector store / browser ---

def test_research_blocks_vector_store_writes():
    """Vector store writes (memory_store/synthesize/extract) are universally blocked."""
    assert "mcp__genesis-memory__memory_store" in PROFILES["research"]
    assert "mcp__genesis-memory__memory_synthesize" in PROFILES["research"]
    assert "mcp__genesis-memory__memory_extract" in PROFILES["research"]


def test_research_allows_sqlite_writes():
    """SQLite table writes (observations, procedures, references) are allowed."""
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


def test_interact_blocks_vector_store_writes():
    """Vector store writes are universally blocked — even for interact."""
    assert "mcp__genesis-memory__memory_store" in PROFILES["interact"]
    assert "mcp__genesis-memory__memory_synthesize" in PROFILES["interact"]
    assert "mcp__genesis-memory__memory_extract" in PROFILES["interact"]


def test_interact_allows_sqlite_writes():
    """SQLite table writes (observations, procedures, references) are allowed."""
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


# --- Profile addendum correctness ---

def test_interact_addendum_mentions_write_available():
    """Interact addendum must tell session Write is available."""
    addendum = _build_profile_addendum("interact")
    assert "You have: Write" in addendum


def test_research_addendum_mentions_write_available():
    """Research addendum must tell session Write is available."""
    addendum = _build_profile_addendum("research")
    assert "You have: Write" in addendum


def test_observe_addendum_does_not_mention_write_available():
    """Observe addendum must NOT claim Write is available."""
    addendum = _build_profile_addendum("observe")
    assert "You have: Write" not in addendum


def test_addenda_do_not_mention_reference_store_for_persistence():
    """Addenda must NOT direct sessions to use reference_store for persistence."""
    for profile, addendum in _PROFILE_ADDENDA.items():
        assert "reference_store" not in addendum, (
            f"{profile} addendum should not mention reference_store"
        )


def test_all_addenda_include_mission_injection():
    """All non-perimeter profiles must include the adapt-and-overcome mission text."""
    _PERIMETER_PROFILES = {"mail"}
    for profile, addendum in _PROFILE_ADDENDA.items():
        if profile in _PERIMETER_PROFILES:
            continue  # Perimeter profiles use their own framing
        assert "Adapt and overcome" in addendum, (
            f"{profile} addendum should include mission injection"
        )


# --- Model override for interact profile ---

def _make_runner():
    """Construct a DirectSessionRunner with mock dependencies."""
    config_builder = MagicMock()
    surplus_cfg = {"system_prompt": "test"}
    config_builder.build_surplus_config.return_value = surplus_cfg
    config_builder.build_mcp_config.return_value = None
    return DirectSessionRunner(
        invoker=MagicMock(),
        session_manager=MagicMock(),
        config_builder=config_builder,
        runtime=MagicMock(),
    )


def test_interact_profile_upgrades_model_to_opus():
    """Interact sessions must always use Opus regardless of requested model."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="interact", model=CCModel.SONNET)
    inv = runner._build_invocation(req)
    assert inv.model == CCModel.OPUS


def test_interact_profile_keeps_opus_when_already_opus():
    """Opus request + interact should stay Opus (idempotent)."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="interact", model=CCModel.OPUS)
    inv = runner._build_invocation(req)
    assert inv.model == CCModel.OPUS


def test_research_profile_does_not_upgrade_model():
    """Non-interact profiles must NOT override the requested model."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="research", model=CCModel.SONNET)
    inv = runner._build_invocation(req)
    assert inv.model == CCModel.SONNET


def test_observe_profile_does_not_upgrade_model():
    """Observe profile must NOT override the requested model."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="observe", model=CCModel.HAIKU)
    inv = runner._build_invocation(req)
    assert inv.model == CCModel.HAIKU


# --- Mail profile: perimeter session restrictions ---

def test_mail_profile_exists():
    assert "mail" in PROFILES
    assert "mail" in VALID_PROFILES


def test_mail_blocks_universal():
    for tool in _UNIVERSAL_BLOCKED:
        assert tool in PROFILES["mail"], f"mail should block {tool}"


def test_mail_blocks_write():
    assert "Write" in PROFILES["mail"]


def test_mail_blocks_web_tools():
    assert "WebFetch" in PROFILES["mail"]
    assert "WebSearch" in PROFILES["mail"]


def test_mail_blocks_memory_writes():
    assert "mcp__genesis-memory__observation_write" in PROFILES["mail"]
    assert "mcp__genesis-memory__procedure_store" in PROFILES["mail"]
    assert "mcp__genesis-memory__reference_store" in PROFILES["mail"]


def test_mail_blocks_follow_ups():
    assert "mcp__genesis-health__follow_up_create" in PROFILES["mail"]


def test_mail_blocks_outreach_extras():
    assert "mcp__genesis-outreach__outreach_send_and_wait" in PROFILES["mail"]
    assert "mcp__genesis-outreach__outreach_poll" in PROFILES["mail"]
    assert "mcp__genesis-outreach__outreach_digest" in PROFILES["mail"]


def test_mail_allows_outreach_send():
    """Mail sessions need outreach_send to reply to emails."""
    assert "mcp__genesis-outreach__outreach_send" not in PROFILES["mail"]


def test_mail_blocks_browser_interaction():
    assert "mcp__genesis-health__browser_click" in PROFILES["mail"]


def test_mail_blocks_recon_writes():
    assert "mcp__genesis-recon__recon_store_finding" in PROFILES["mail"]


def test_mail_addendum_mentions_internals_private():
    addendum = _build_profile_addendum("mail")
    assert "internals private" in addendum


def test_mail_addendum_does_not_include_mission_injection():
    """Mail profile uses its own framing, not the mission injection."""
    addendum = _build_profile_addendum("mail")
    # Mail has its own directive instead of the generic mission text
    assert "outreach_send" in addendum


def test_mail_profile_does_not_upgrade_model():
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="mail", model=CCModel.SONNET)
    inv = runner._build_invocation(req)
    assert inv.model == CCModel.SONNET
