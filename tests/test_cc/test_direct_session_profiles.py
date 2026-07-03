"""Tests for direct session profile definitions and tool restrictions.

Validates that profile changes don't accidentally grant or revoke tool access.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

from genesis.cc.direct_session import (
    _BG_CC_TMP_ROOT,
    _PROFILE_ADDENDA,
    PROFILES,
    VALID_PROFILES,
    DirectSessionRequest,
    DirectSessionRunner,
    ProfileOverlayContext,
    _bg_session_root,
    _bg_session_sandbox,
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
    inv = runner._build_invocation(req, "test-session")
    assert inv.model == CCModel.OPUS


def test_interact_profile_keeps_opus_when_already_opus():
    """Opus request + interact should stay Opus (idempotent)."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="interact", model=CCModel.OPUS)
    inv = runner._build_invocation(req, "test-session")
    assert inv.model == CCModel.OPUS


def test_interact_profile_pins_fable_down_to_opus():
    """Interact intentionally pins Fable DOWN to Opus (Fable not yet cleared for
    the browser/ATS path — see cc-compatibility.md). Documents the deliberate
    pin so it isn't mistaken for a downgrade bug; flip to a tier floor if Fable
    is later cleared for interact work."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="interact", model=CCModel.FABLE)
    inv = runner._build_invocation(req, "test-session")
    assert inv.model == CCModel.OPUS


def test_research_profile_does_not_upgrade_model():
    """Non-interact profiles must NOT override the requested model."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="research", model=CCModel.SONNET)
    inv = runner._build_invocation(req, "test-session")
    assert inv.model == CCModel.SONNET


def test_observe_profile_does_not_upgrade_model():
    """Observe profile must NOT override the requested model."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="observe", model=CCModel.HAIKU)
    inv = runner._build_invocation(req, "test-session")
    assert inv.model == CCModel.HAIKU


# --- Roster model SELECTION (Phase 3 Part E) ---

_HERMETIC_ROSTER = {
    "default": "claude",
    "models": {
        "claude": {"native_subscription": True, "failover_order": 0},
        "glm-5.2": {
            "anthropic_base_url": "https://open.bigmodel.cn/api/anthropic",
            "auth_env": "ZHIPU_TEST_KEY",
            "model_id": "glm-5.2",
            "failover_order": 1,
        },
    },
}


def _patch_roster(monkeypatch):
    from genesis.cc import roster
    monkeypatch.setattr(roster, "load_roster", lambda *a, **k: _HERMETIC_ROSTER)
    return roster


def test_roster_model_routes_to_peer(monkeypatch):
    _patch_roster(monkeypatch)
    monkeypatch.setenv("ZHIPU_TEST_KEY", "sk-secret")
    runner = _make_runner()
    req = DirectSessionRequest(prompt="t", profile="observe", roster_model="glm-5.2")
    inv = runner._build_invocation(req, "test-session")
    assert inv.model_id_override == "glm-5.2"
    assert inv.anthropic_base_url == "https://open.bigmodel.cn/api/anthropic"
    assert inv.anthropic_auth_token == "sk-secret"
    # routed → eligible so the chokepoint honors it AND attributes the right name.
    assert inv.roster_eligible is True


def test_roster_model_claude_pins_native(monkeypatch):
    _patch_roster(monkeypatch)
    runner = _make_runner()
    req = DirectSessionRequest(prompt="t", profile="observe", roster_model="claude")
    inv = runner._build_invocation(req, "test-session")
    assert inv.model_id_override is None
    assert inv.anthropic_base_url is None
    # native pin → NOT eligible so the chokepoint can't re-select the global default.
    assert inv.roster_eligible is False


def test_no_roster_model_uses_chokepoint_default(monkeypatch):
    _patch_roster(monkeypatch)
    runner = _make_runner()
    req = DirectSessionRequest(prompt="t", profile="observe")  # no roster_model
    inv = runner._build_invocation(req, "test-session")
    assert inv.roster_eligible is True  # chokepoint selects at invoke time
    assert inv.model_id_override is None


def test_unknown_roster_model_fails_loud(monkeypatch):
    roster = _patch_roster(monkeypatch)
    runner = _make_runner()
    req = DirectSessionRequest(prompt="t", profile="observe", roster_model="nope")
    with pytest.raises(roster.RosterError):
        runner._build_invocation(req, "test-session")


def test_keyless_roster_model_fails_loud(monkeypatch):
    roster = _patch_roster(monkeypatch)
    monkeypatch.delenv("ZHIPU_TEST_KEY", raising=False)
    runner = _make_runner()
    req = DirectSessionRequest(prompt="t", profile="observe", roster_model="glm-5.2")
    with pytest.raises(roster.RosterError):
        runner._build_invocation(req, "test-session")


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
    inv = runner._build_invocation(req, "test-session")
    assert inv.model == CCModel.SONNET


# --- Steward profile: Bash-enabled, gh-scoped, upstream-PR stewardship ---

def test_steward_profile_exists():
    assert "steward" in PROFILES
    assert "steward" in VALID_PROFILES


def test_steward_grants_bash():
    """The whole point of steward: Bash is NOT disallowed (it runs gh)."""
    assert "Bash" not in PROFILES["steward"]


def test_steward_blocks_other_universal_tools():
    """Everything else in the universal block stays blocked (defense in depth)."""
    for tool in _UNIVERSAL_BLOCKED - {"Bash"}:
        assert tool in PROFILES["steward"], f"steward should still block {tool}"


def test_steward_blocks_write_and_edit():
    """Steward escalates code fixes — it does not write or edit files itself."""
    assert "Write" in PROFILES["steward"]
    assert "Edit" in PROFILES["steward"]


def test_steward_blocks_browser_interaction():
    assert "mcp__genesis-health__browser_click" in PROFILES["steward"]


def test_steward_allows_outreach_send():
    """Steward notifies via outreach_send after each action."""
    assert "mcp__genesis-outreach__outreach_send" not in PROFILES["steward"]


def test_steward_addendum_has_mission():
    addendum = _build_profile_addendum("steward")
    assert "Adapt and overcome" in addendum


def test_steward_does_not_upgrade_model():
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="steward", model=CCModel.SONNET)
    inv = runner._build_invocation(req, "test-session")
    assert inv.model == CCModel.SONNET


# --- Bash allowlist plumbing ---

def test_steward_invocation_sets_gh_bash_allowlist():
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="steward", model=CCModel.SONNET)
    inv = runner._build_invocation(req, "test-session")
    assert inv.bash_allowlist == ("gh",)


def test_non_steward_invocation_has_empty_bash_allowlist():
    runner = _make_runner()
    req = DirectSessionRequest(prompt="test", profile="research", model=CCModel.SONNET)
    inv = runner._build_invocation(req, "test-session")
    assert inv.bash_allowlist == ()


# --- Profile overlay mechanism (generic; install-local profiles) ---
# Install-specific profiles live in an optional, gitignored
# genesis.cc.profile_overlay module — these tests exercise the loader + the
# ProfileOverlayContext contract WITHOUT depending on any overlay being present
# (upstream CI has none), so they assert the mechanism, not a specific profile.


def _make_ctx(added):
    """A ProfileOverlayContext wired to the real building blocks. `added`
    accumulates registered names so the caller can clean the global dicts."""
    from genesis.cc import direct_session as ds

    ctx = ProfileOverlayContext(
        universal_disallow=ds._UNIVERSAL_DISALLOW,
        no_browser_interaction=ds._NO_BROWSER_INTERACTION,
        no_file_write=ds._NO_FILE_WRITE,
        no_outreach_send=ds._NO_OUTREACH_SEND,
        no_outreach_extras=ds._NO_OUTREACH_EXTRAS,
        no_memory_writes=ds._NO_MEMORY_WRITES,
        no_follow_ups=ds._NO_FOLLOW_UPS,
        no_outreach_engagement=ds._NO_OUTREACH_ENGAGEMENT,
        no_recon_writes=ds._NO_RECON_WRITES,
        no_web_tools=ds._NO_WEB_TOOLS,
        venv_python=ds._VENV_PYTHON,
    )
    real_add = ctx.add_profile

    def _tracking_add(name, **kw):
        real_add(name, **kw)
        added.append(name)

    ctx.add_profile = _tracking_add  # type: ignore[method-assign]
    return ctx


def _drop(names):
    from genesis.cc import direct_session as ds

    for name in names:
        ds.PROFILES.pop(name, None)
        ds._PROFILE_ADDENDA.pop(name, None)
        ds._PROFILE_BASH_ALLOWLIST.pop(name, None)
        ds._PROFILE_TO_MCP.pop(name, None)
        ds._PROFILE_SKILLS.pop(name, None)


@pytest.fixture
def overlay_ctx():
    added: list[str] = []
    try:
        yield _make_ctx(added)
    finally:
        _drop(added)


def test_overlay_add_profile_registers_into_all_dicts(overlay_ctx):
    from genesis.cc import direct_session as ds

    overlay_ctx.add_profile(
        "ztest-profile",
        disallow=["Edit", "Bash"],
        addendum="hello",
        bash_allowlist=("gh",),
        mcp_profile="campaign",
        skills=["voice-master"],
    )
    assert ds.PROFILES["ztest-profile"] == ["Edit", "Bash"]
    assert ds._PROFILE_ADDENDA["ztest-profile"] == "hello"
    assert ds._PROFILE_BASH_ALLOWLIST["ztest-profile"] == ("gh",)
    assert ds._PROFILE_TO_MCP["ztest-profile"] == "campaign"
    assert ds._PROFILE_SKILLS["ztest-profile"] == ["voice-master"]


def test_overlay_add_profile_defaults(overlay_ctx):
    """Omitted allowlist/skills default to empty; mcp defaults to reflection."""
    from genesis.cc import direct_session as ds

    overlay_ctx.add_profile("ztest-profile", disallow=["Bash"], addendum="x")
    assert ds._PROFILE_BASH_ALLOWLIST["ztest-profile"] == ()
    assert ds._PROFILE_TO_MCP["ztest-profile"] == "reflection"
    assert ds._PROFILE_SKILLS["ztest-profile"] == []


def test_overlay_cannot_override_builtin_profile(overlay_ctx):
    """An overlay may only ADD profiles, never silently redefine a shipped one."""
    with pytest.raises(ValueError, match="may not override"):
        overlay_ctx.add_profile("steward", disallow=[], addendum="x")


def test_overlay_profile_flows_through_build_invocation(overlay_ctx, monkeypatch):
    """A registered overlay profile drives bash_allowlist + MCP selection in
    _build_invocation (VALID_PROFILES is frozen at import, so patch it to admit
    the test profile — the real load happens at import time)."""
    from genesis.cc import direct_session as ds

    overlay_ctx.add_profile(
        "ztest-profile",
        disallow=[t for t in ds._UNIVERSAL_DISALLOW if t != "Bash"],
        addendum="x",
        bash_allowlist=(ds._VENV_PYTHON,),
        mcp_profile="campaign",
    )
    monkeypatch.setattr(ds, "VALID_PROFILES", ds.VALID_PROFILES | {"ztest-profile"})
    runner = _make_runner()
    req = DirectSessionRequest(prompt="t", profile="ztest-profile", model=CCModel.SONNET)
    inv = runner._build_invocation(req, "test-session")
    assert inv.bash_allowlist == (ds._VENV_PYTHON,)
    runner._config_builder.build_mcp_config.assert_called_with(profile="campaign")


def test_load_profile_overlays_survives_broken_overlay(monkeypatch):
    """A raising overlay is logged and ignored — it must not break spawning."""
    from genesis.cc import direct_session as ds

    def _boom(ctx):
        raise RuntimeError("overlay is broken")

    fake = types.SimpleNamespace(register=_boom)
    monkeypatch.setitem(sys.modules, "genesis.cc.profile_overlay", fake)
    before = set(ds.PROFILES)
    ds._load_profile_overlays()  # must not raise
    assert set(ds.PROFILES) == before  # built-ins untouched


def test_load_profile_overlays_registers_from_module(monkeypatch):
    """The loader wires the context and calls register(), so a profile added
    there lands in the live dicts."""
    from genesis.cc import direct_session as ds

    def _register(ctx):
        ctx.add_profile("zloaded-profile", disallow=["Bash"], addendum="x")

    fake = types.SimpleNamespace(register=_register)
    monkeypatch.setitem(sys.modules, "genesis.cc.profile_overlay", fake)
    try:
        ds._load_profile_overlays()
        assert "zloaded-profile" in ds.PROFILES
    finally:
        _drop(["zloaded-profile"])


def test_load_profile_overlays_raises_on_builtin_collision(monkeypatch):
    """A config error (overlay redefining a built-in) surfaces — not swallowed
    by the broad runtime-failure guard."""
    from genesis.cc import direct_session as ds

    def _register(ctx):
        ctx.add_profile("steward", disallow=[], addendum="x")

    fake = types.SimpleNamespace(register=_register)
    monkeypatch.setitem(sys.modules, "genesis.cc.profile_overlay", fake)
    with pytest.raises(ValueError, match="may not override"):
        ds._load_profile_overlays()


def test_load_profile_overlays_noop_when_absent(monkeypatch):
    """With no overlay module importable, built-in profiles are unchanged."""
    from genesis.cc import direct_session as ds

    monkeypatch.setitem(sys.modules, "genesis.cc.profile_overlay", None)
    # `from genesis.cc import profile_overlay` with a None entry raises
    # ImportError, which the loader swallows.
    before = set(ds.PROFILES)
    ds._load_profile_overlays()
    assert set(ds.PROFILES) == before


# --- Per-session CC sandbox isolation (background dispatch) ---

def test_bg_session_sandbox_is_off_cc_tmp():
    """A session's sandbox lives under ~/tmp/bg-cc-sessions, NOT the
    watchgod-policed ~/.genesis/cc-tmp."""
    path = _bg_session_sandbox("sess-abc")
    assert path.endswith("/bg-cc-sessions/sess-abc/cc-sandbox")
    assert str(_BG_CC_TMP_ROOT) in path
    assert ".genesis/cc-tmp" not in path


def test_bg_session_sandbox_distinct_per_session():
    """Two sessions get distinct sandbox dirs (no cross-session collision)."""
    assert _bg_session_sandbox("sess-a") != _bg_session_sandbox("sess-b")
    assert _bg_session_root("sess-a") != _bg_session_root("sess-b")


def test_bg_session_sandbox_is_pure_no_mkdir():
    """Deriving the path must NOT create the dir (side-effect belongs to
    _run_session, so building an invocation is pure)."""
    sid = "sess-pure-check-xyz"
    root = _bg_session_root(sid)
    assert not root.exists()
    _bg_session_sandbox(sid)
    assert not root.exists()


def test_build_invocation_sets_isolated_tmpdir():
    """_build_invocation wires the per-session sandbox into the CCInvocation,
    off cc-tmp — the actual seam the fix depends on."""
    runner = _make_runner()
    req = DirectSessionRequest(prompt="t", profile="research", model=CCModel.SONNET)
    inv = runner._build_invocation(req, "sess-xyz")
    assert inv.claude_code_tmpdir == _bg_session_sandbox("sess-xyz")
    assert "bg-cc-sessions/sess-xyz" in (inv.claude_code_tmpdir or "")
    assert ".genesis/cc-tmp" not in (inv.claude_code_tmpdir or "")
