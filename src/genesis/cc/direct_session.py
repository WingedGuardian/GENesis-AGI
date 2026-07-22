"""DirectSessionRunner — directed background CC session spawner.

"Run this prompt, report back." No task decomposition, no adversarial
review, no surplus routing. Profile-constrained CC session with
tool-level safety enforcement. Results delivered via Telegram.

Profiles restrict tool access via CC's ``disallowed_tools`` mechanism,
which removes tools from the model's view entirely (validated: CC strips
them from the init tools list).

Design notes (from architect review):
- Uses a *dedicated* CCInvoker instance (not the shared one) because
  ``_active_proc`` is not concurrency-safe under Semaphore(2).
- Accesses ``outreach_pipeline`` lazily via runtime ref (not injected at
  init time) because outreach init runs after cc_relay in bootstrap.
- Never calls ``should_throttle`` — this is user-invoked work, not
  autonomous background compute. Cost control is the user's decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.cc import rate_limit_park, roster
from genesis.cc.exceptions import CCQuotaExhaustedError, CCRateLimitError
from genesis.cc.types import (
    CCInvocation,
    CCModel,
    CCOutput,
    DeliveryMode,
    EffortLevel,
    SessionType,
    StreamEvent,
    background_session_dir,
    cc_project_key,
    origin_delivery_supported,
)
from genesis.observability.session_context import set_session_id as _set_obs_session
from genesis.util.tasks import tracked_task

if TYPE_CHECKING:
    from genesis.cc import AgentProvider
    from genesis.cc.session_config import SessionConfigBuilder
    from genesis.cc.session_manager import SessionManager
    from genesis.ego.verification import VerificationResult

logger = logging.getLogger(__name__)

# Appended to a background session's delivered result when the CLI truncated its
# dispatched Workflow/subagent work at the wait ceiling (CCOutput.bg_truncated).
# The run still delivers its partial output (success stays True — a deliverable
# was produced); the notice keeps that honest rather than shipping it as complete.
_BG_TRUNCATION_NOTICE = (
    "\n\n⚠️ Note: this run reached its time budget and some background work was "
    "cut off before finishing, so the results below may be incomplete."
)

# Soft cap for a single Telegram delivery of a RESULT-mode outcome. Below the
# 4096-char hard limit, leaving room for the header + HTML entity expansion.
# Larger output is written to a file and delivered as a head + pointer.
_TG_MSG_CAP = 3500


def _write_result_artifact(session_id: str, raw: str) -> str | None:
    """Write a background session's full result to ``~/.genesis/output`` so an
    oversized outcome can be delivered as a head + file pointer instead of a
    wall of Telegram messages. Returns the path, or None if the write failed
    (delivery still proceeds with just the truncated head).
    """
    try:
        out_dir = Path.home() / ".genesis" / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"bg-session-{session_id}.md"
        path.write_text(raw, encoding="utf-8")
        return str(path)
    except Exception:
        logger.error(
            "Failed to write result artifact for %s",
            session_id[:8],
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Per-session CC Bash-sandbox isolation (background dispatch sessions)
# ---------------------------------------------------------------------------
# By default a background session's CC Bash sandbox (CLAUDE_CODE_TMPDIR) lives in
# the shared, watchgod-policed ~/.genesis/cc-tmp. Giving each session its OWN
# sandbox under ~/tmp (OFF cc-tmp) means (a) its scratch can't be clipped by
# tmp_watchgod's RED nuclear-cleanup mid-run, and (b) it stops contributing to
# cc-tmp pressure that could trip cleanup of foreground CLI sessions. Mirrors the
# gauntlet (eval/gauntlet.py). This overrides CLAUDE_CODE_TMPDIR — the CC-specific
# per-invocation sandbox var — NOT the shell TMPDIR, which the `tmp_filesystem_limit`
# procedure correctly says never to override globally. (It does NOT prevent a
# "kill": background sessions are asyncio subprocesses, not tmux sessions, so
# watchgod's tmux-kill can't reach them anyway — do not claim otherwise.)
_BG_CC_TMP_ROOT = Path.home() / "tmp" / "bg-cc-sessions"


def _bg_session_root(session_id: str) -> Path:
    """Per-session root dir for a background CC session's isolated sandbox."""
    return _BG_CC_TMP_ROOT / session_id


def _bg_session_sandbox(session_id: str) -> str:
    """Return this session's CLAUDE_CODE_TMPDIR path (off cc-tmp).

    Pure — the caller (_run_session) creates the directory just before the
    session runs, so building an invocation has no filesystem side effect.
    """
    return str(_bg_session_root(session_id) / "cc-sandbox")


# ---------------------------------------------------------------------------
# Tool profiles — disallowed tools per profile
# ---------------------------------------------------------------------------
# CC removes disallowed tools from the model's view. The model literally
# cannot see or call them. Validated empirically: the init event's tools
# list shrinks by the disallowed count.

_UNIVERSAL_DISALLOW = [
    "Bash",
    "Edit",
    "NotebookEdit",
    "mcp__genesis-health__task_submit",
    "mcp__genesis-health__settings_update",
    "mcp__genesis-health__direct_session_run",  # No recursive spawn
    "mcp__genesis-health__module_call",
    # ── Vector store isolation ────────────────────────────────────
    # Background sessions MUST NOT write to Qdrant (episodic_memory
    # or knowledge_base). Episodic memory is exclusively for
    # foreground user interactions. Background findings belong in
    # the session transcript (the deliverable) or in SQLite tables
    # (observations, references, follow-ups) — never in vector stores.
    # Server-side code (ego corrections, reflection output) uses
    # MemoryStore directly and is unaffected by tool-level blocking.
    "mcp__genesis-memory__memory_store",
    "mcp__genesis-memory__memory_synthesize",
    "mcp__genesis-memory__memory_extract",
    # Knowledge ingestion requires explicit user authorization.
    "mcp__genesis-memory__knowledge_ingest",
    "mcp__genesis-memory__knowledge_ingest_batch",
    "mcp__genesis-memory__knowledge_ingest_source",
]

_NO_OUTREACH_SEND = [
    "mcp__genesis-outreach__outreach_send",
    "mcp__genesis-outreach__outreach_send_and_wait",
]

# Composable building blocks for profile disallow lists
_NO_BROWSER_INTERACTION = [
    "mcp__genesis-health__browser_click",
    "mcp__genesis-health__browser_fill",
    "mcp__genesis-health__browser_run_js",
    "mcp__genesis-health__browser_clear_domain",
    "mcp__genesis-health__browser_collaborate",
]

_NO_FILE_WRITE = [
    "Write",  # Moved out of _UNIVERSAL_DISALLOW so interact/research can use it
]

_NO_MEMORY_WRITES = [
    # memory_store/synthesize/extract + knowledge_ingest* are in
    # _UNIVERSAL_DISALLOW (vector store isolation).
    # This list covers SQLite-table writes blocked only for observe.
    "mcp__genesis-memory__observation_write",
    "mcp__genesis-memory__observation_resolve",
    "mcp__genesis-memory__procedure_store",
    "mcp__genesis-memory__reference_store",
    "mcp__genesis-memory__reference_delete",
]

_NO_FOLLOW_UPS = [
    "mcp__genesis-health__follow_up_create",
    "mcp__genesis-health__follow_up_update",
]

_NO_OUTREACH_ENGAGEMENT = [
    "mcp__genesis-outreach__outreach_engagement",
    "mcp__genesis-outreach__outreach_preferences",
    "mcp__genesis-outreach__outreach_queue",
]

_NO_RECON_WRITES = [
    "mcp__genesis-recon__recon_store_finding",
    "mcp__genesis-recon__recon_run_model_intelligence",
]

# Perimeter sessions: block web tools to prevent second-stage content
# retrieval from attacker-controlled URLs.
_NO_WEB_TOOLS = [
    "WebFetch",
    "WebSearch",
]

# Perimeter sessions: block outreach tools beyond basic send.
_NO_OUTREACH_EXTRAS = [
    "mcp__genesis-outreach__outreach_send_and_wait",
    "mcp__genesis-outreach__outreach_poll",
    "mcp__genesis-outreach__outreach_digest",
]

# The venv Python interpreter running genesis-server. Exposed to profile
# overlays (see _load_profile_overlays) so a locally-defined Bash profile can
# allowlist exactly this path and run `<this> -m <module>`. Using
# sys.executable keeps it install-agnostic (no hard-coded home path).
_VENV_PYTHON = sys.executable

PROFILES: dict[str, list[str]] = {
    "observe": (
        _UNIVERSAL_DISALLOW
        + _NO_FILE_WRITE
        + _NO_OUTREACH_SEND
        + _NO_BROWSER_INTERACTION
        + _NO_MEMORY_WRITES
        + _NO_FOLLOW_UPS
        + _NO_OUTREACH_ENGAGEMENT
        + _NO_RECON_WRITES
    ),
    "interact": (_UNIVERSAL_DISALLOW + _NO_OUTREACH_ENGAGEMENT + _NO_RECON_WRITES),
    "research": (_UNIVERSAL_DISALLOW + _NO_OUTREACH_SEND + _NO_BROWSER_INTERACTION),
    "campaign": (_UNIVERSAL_DISALLOW + _NO_BROWSER_INTERACTION),
    # ── Steward profile ──────────────────────────────────────────
    # For the upstream-PR stewardship campaign. UNIQUE among profiles: it
    # grants Bash (so it can run `gh`) — every other profile blocks Bash.
    # The shell is NOT open, though: scripts/bash_safety_hook.sh restricts it
    # to the `gh` binary only (via GENESIS_BASH_ALLOWLIST). Write/Edit/browser
    # stay blocked — the campaign comments/reopens/closes PRs and ESCALATES
    # code fixes rather than editing/pushing itself.
    "steward": (
        [t for t in _UNIVERSAL_DISALLOW if t != "Bash"] + _NO_BROWSER_INTERACTION + _NO_FILE_WRITE
    ),
    # ── Community responder profile ─────────────────────────────
    # Reactive community responder: reads a community's channels and replies
    # via the discord-bot MCP server. MCP config loads discord-bot + health +
    # outreach (no memory server). Belt-and-suspenders: block memory writes at
    # tool level too, in case MCP config generation fails and falls back to full.
    "community-responder": (
        _UNIVERSAL_DISALLOW + _NO_BROWSER_INTERACTION + _NO_MEMORY_WRITES + _NO_FOLLOW_UPS
    ),
    # ── Perimeter profile ────────────────────────────────────────
    # For sessions that process untrusted inbound content (email
    # replies, future Discord inbound). Maximally restricted: only
    # outreach_send is available. MCP config loads genesis-outreach
    # only — no memory or health servers. Belt-and-suspenders: tools
    # are also listed here in case MCP config is misconfigured.
    "mail": (
        _UNIVERSAL_DISALLOW
        + _NO_FILE_WRITE
        + _NO_BROWSER_INTERACTION
        + _NO_MEMORY_WRITES
        + _NO_FOLLOW_UPS
        + _NO_OUTREACH_ENGAGEMENT
        + _NO_RECON_WRITES
        + _NO_WEB_TOOLS
        + _NO_OUTREACH_EXTRAS
    ),
}


# ---------------------------------------------------------------------------
# Profile addendum + skill auto-injection
# ---------------------------------------------------------------------------

_MISSION_INJECTION = (
    "If it's digital, it's within reach. Adapt and overcome. "
    'Don\'t conclude "impossible" — conclude "blocked by X" and identify what would unblock it.'
)

_PROFILE_ADDENDA: dict[str, str] = {
    "interact": f"""

## Session Profile: interact

You have: Write, browser MCP tools, memory MCP tools, outreach send.
You do NOT have: Edit, Bash, NotebookEdit.
Your final message IS your deliverable. Write files to `~/.genesis/output/`.

{_MISSION_INJECTION}
""",
    "research": f"""

## Session Profile: research

You have: Write, memory MCP tools, web tools (web_search, web_fetch).
You do NOT have: Edit, Bash, NotebookEdit, browser tools.
Your final message IS your deliverable. Write files to `~/.genesis/output/`.

{_MISSION_INJECTION}
""",
    "observe": f"""

## Session Profile: observe

You have: memory MCP tools (read-only).
You do NOT have: Write, Edit, Bash, NotebookEdit, browser tools.
Your final message IS your deliverable.

{_MISSION_INJECTION}
""",
    "campaign": f"""

## Session Profile: campaign

You have: Write, memory MCP tools, web tools, outreach_send.
You do NOT have: Edit, Bash, NotebookEdit, browser tools.
Your final message IS your deliverable. Write files to `~/.genesis/output/`.

{_MISSION_INJECTION}
""",
    "steward": f"""

## Session Profile: steward

You have: Bash (restricted to the `gh` CLI only), memory MCP tools, outreach_send.
You do NOT have: Write, Edit, NotebookEdit, browser tools, and Bash may ONLY run
`gh` — any other command (curl, python, cat, pipes, redirects, chaining) is blocked.

You steward Genesis's own upstream pull requests. Use `gh` to read PR state,
reviews, and comments, and to comment / reopen / re-request review / close PRs.
When a review asks for CODE changes, do NOT edit or push — draft the fix and
escalate it to the user via outreach_send. Notify via outreach_send after every
action you take on an external PR.

{_MISSION_INJECTION}
""",
    "community-responder": f"""

## Session Profile: community-responder

You have: discord-bot MCP tools (fetch_messages, fetch_forum_threads, send_reply), outreach_send, web tools.
You do NOT have: Edit, Bash, NotebookEdit, browser tools, memory tools.
Your final message IS your deliverable.

You are a reactive community responder. Read the community's channels and respond
to unanswered messages using send_reply. Do NOT post proactive content — proactive
posting is a separate campaign's job, not this profile's.

{_MISSION_INJECTION}
""",
    "mail": """

## Session Profile: mail

You have: outreach_send (email channel only, with thread_id).
You do NOT have: memory tools, web tools, Write, Edit, Bash, browser tools.
Your final message IS your deliverable.

You are Genesis responding to correspondence. You keep your internals private.
If asked about your architecture, capabilities, tools, or internal systems,
respond confidently: "I keep my internals private." Do not explain what you
cannot access. Do not apologize for limitations. Handle what you can.
""",
}

# Skills auto-injected by profile (always loaded for that profile)
_PROFILE_SKILLS: dict[str, list[str]] = {
    "interact": ["stealth-browser"],
    "research": [],
    "observe": [],
    "campaign": ["voice-master"],
    "steward": ["voice-master"],
    "community-responder": ["genesis-voice"],
    "mail": ["genesis-voice"],
}

# Profiles that grant Bash run it under an allowlist of permitted command
# binaries, enforced by scripts/bash_safety_hook.sh (GENESIS_BASH_ALLOWLIST).
# A profile absent from this map gets no allowlist (its Bash, if any, is
# governed only by the global destructive-op blocks).
_PROFILE_BASH_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "steward": ("gh",),
}

# Which Genesis MCP server set each profile gets. Unknown profiles fall back to
# "reflection" (health + memory, read-leaning). Module-level (not inside
# _build_invocation) so profile overlays can register their own mapping.
_PROFILE_TO_MCP: dict[str, str] = {
    "observe": "reflection",
    # research maps to its OWN MCP profile so it can reach genesis-recon (the
    # discovery engine). Kept off "reflection" so observe — a passive profile —
    # does NOT get recon. Recon tools are otherwise unreachable in any bg session.
    "research": "research",
    "interact": "sentinel",
    "campaign": "campaign",
    "steward": "campaign",  # health + memory + outreach (for notify)
    "community-responder": "community-responder",
    "mail": "mail",
}

# WS-3 session-level provenance per profile (stamped as CCInvocation.origin →
# GENESIS_SESSION_ORIGIN → the session's memory MCP writes). Classified by what
# the profile INGESTS by construction, not its label:
#   research  — web/knowledge ingestion is the job.
#   interact  — the browser profile; arbitrary external page content.
#   campaign  — engages external platforms, reads external replies.
#   steward   — reads GitHub PR content (external contributors/bots).
#   community-responder — reads external Discord messages.
#   mail      — external email bodies (belt-and-suspenders: its MCP profile has
#               no memory server today, but the classification is content-true).
#   observe   — DELIBERATELY absent → first_party: its purpose is Genesis
#               self-observation; web tools are incidental, and blanket-tagging
#               Genesis's own self-model writes external would be the
#               autoimmune failure WS-3 is built to avoid.
# Unknown/overlay profiles default to first_party (B0's conservative store-time
# stance); tests/test_cc/test_direct_session_profiles.py forces every
# registered profile to be classified here or in the explicit first-party set.
_PROFILE_ORIGIN: dict[str, str] = {
    "research": "external_untrusted",
    "interact": "external_untrusted",
    "campaign": "external_untrusted",
    "steward": "external_untrusted",
    "community-responder": "external_untrusted",
    "mail": "external_untrusted",
}

# Profiles deliberately classified first-party (origin left unset) — with the
# reason above. The coverage test asserts _PROFILE_TO_MCP ⊆
# (_PROFILE_ORIGIN ∪ _PROFILE_ORIGIN_FIRST_PARTY) so a new profile cannot ship
# unclassified.
_PROFILE_ORIGIN_FIRST_PARTY: frozenset[str] = frozenset({"observe"})


# ---------------------------------------------------------------------------
# Profile overlays — install-local profile registration
# ---------------------------------------------------------------------------
# A deployment can define extra background-session profiles (including
# Bash-scoped ones) WITHOUT editing this tracked file, by providing an optional
# ``genesis.cc.profile_overlay`` module that exposes ``register(ctx)``. This
# mirrors how install-local modules plug in via the module registry: the
# install-specific profile (its name, addendum, tool scope) stays local and
# gitignored, while this generic loader is the only thing that ships upstream.


@dataclass
class ProfileOverlayContext:
    """Handed to a profile overlay's ``register(ctx)`` so it can compose a
    profile from the same building blocks the built-in profiles use, then
    register it via :meth:`add_profile` — no need to import this module's
    internals or duplicate the universal safety blocks."""

    universal_disallow: list[str]
    no_browser_interaction: list[str]
    no_file_write: list[str]
    no_outreach_send: list[str]
    no_outreach_extras: list[str]
    no_memory_writes: list[str]
    no_follow_ups: list[str]
    no_outreach_engagement: list[str]
    no_recon_writes: list[str]
    no_web_tools: list[str]
    venv_python: str

    def add_profile(
        self,
        name: str,
        *,
        disallow: list[str],
        addendum: str,
        bash_allowlist: tuple[str, ...] = (),
        mcp_profile: str = "reflection",
        skills: list[str] | None = None,
    ) -> None:
        """Register one overlay profile into the live profile dicts.

        Refuses to clobber a built-in profile name, so an overlay can only add,
        never silently redefine a shipped profile's tool scope.
        """
        if name in PROFILES:
            raise ValueError(f"profile overlay may not override built-in profile {name!r}")
        PROFILES[name] = list(disallow)
        _PROFILE_ADDENDA[name] = addendum
        _PROFILE_BASH_ALLOWLIST[name] = tuple(bash_allowlist)
        _PROFILE_TO_MCP[name] = mcp_profile
        _PROFILE_SKILLS[name] = list(skills or [])


def _load_profile_overlays() -> None:
    """Merge install-local profiles from an optional ``genesis.cc.profile_overlay``.

    No-op when the module is absent (the upstream/default case). Failures are
    logged but never fatal — a broken overlay must not take down session
    spawning entirely.
    """
    import importlib

    try:
        profile_overlay = importlib.import_module("genesis.cc.profile_overlay")
    except ImportError:
        return
    ctx = ProfileOverlayContext(
        universal_disallow=_UNIVERSAL_DISALLOW,
        no_browser_interaction=_NO_BROWSER_INTERACTION,
        no_file_write=_NO_FILE_WRITE,
        no_outreach_send=_NO_OUTREACH_SEND,
        no_outreach_extras=_NO_OUTREACH_EXTRAS,
        no_memory_writes=_NO_MEMORY_WRITES,
        no_follow_ups=_NO_FOLLOW_UPS,
        no_outreach_engagement=_NO_OUTREACH_ENGAGEMENT,
        no_recon_writes=_NO_RECON_WRITES,
        no_web_tools=_NO_WEB_TOOLS,
        venv_python=_VENV_PYTHON,
    )
    try:
        profile_overlay.register(ctx)
    except ValueError:
        # Config error (e.g. an overlay trying to redefine a built-in profile)
        # — surface it loudly at startup rather than silently dropping a
        # profile the operator believes is registered.
        raise
    except Exception:  # pragma: no cover - defensive; never block spawning
        logger.exception("profile overlay registration failed; ignoring overlay")


_load_profile_overlays()

# Computed AFTER overlay registration so locally-added profiles validate.
VALID_PROFILES = frozenset(PROFILES.keys())

# Keyword triggers for content-related skills (scanned against prompt)
_CONTENT_SKILL_TRIGGERS: list[tuple[list[str], list[str]]] = [
    (
        ["publish", "article", "medium", "content", "post", "draft", "blog"],
        ["content-publish", "voice-master"],
    ),
]


def _build_profile_addendum(profile: str) -> str:
    """Return the profile constraint addendum for background sessions."""
    return _PROFILE_ADDENDA.get(profile, _PROFILE_ADDENDA["observe"])


def _resolve_skills(request: DirectSessionRequest) -> list[str]:
    """Determine which skills to inject: explicit > profile + auto-detect."""
    if request.skills is not None:
        return request.skills

    # Start with profile-bound skills
    skills = list(_PROFILE_SKILLS.get(request.profile, []))

    # Scan prompt for keyword triggers
    prompt_lower = request.prompt.lower()
    for keywords, skill_names in _CONTENT_SKILL_TRIGGERS:
        if any(kw in prompt_lower for kw in keywords):
            for name in skill_names:
                if name not in skills:
                    skills.append(name)

    return skills


# ---------------------------------------------------------------------------
# Request / Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectSessionRequest:
    """What to run in the background session."""

    prompt: str
    profile: str = "observe"
    model: CCModel = CCModel.SONNET
    effort: EffortLevel = EffortLevel.HIGH
    system_prompt: str | None = None  # None = SOUL.md identity
    timeout_s: int = 3600  # 1 hour — model decides when to stop
    notify: bool = True
    notify_on_failure_only: bool = False
    source_tag: str = "direct_session"
    caller_context: str | None = None  # "follow_up:<id>", "schedule:<id>"
    planning_instruction: str | None = None  # opt-in: prepended to prompt
    skills: list[str] | None = None  # explicit skill injection (overrides auto-detect)
    tool_exceptions: tuple[str, ...] = ()  # tools to UN-block from the profile disallow list
    # Intentional per-dispatch model SELECTION (not failover): a roster name
    # (e.g. "glm-5.2") to run this background session on instead of the global
    # default. None → the chokepoint applies the active default as usual.
    roster_model: str | None = None
    # Delivery of the terminal outcome. None → derived from the legacy
    # notify/notify_on_failure_only bools in __post_init__ (SILENT/FAILURE_ONLY),
    # so existing callers are unchanged. RESULT is set only by the
    # deliver_to_origin dispatch path and requires origin_session_id.
    delivery_mode: DeliveryMode | None = None
    # The foreground cc_sessions row id this session was dispatched from, used
    # to route a RESULT delivery back to that conversation. Captured from
    # GENESIS_SESSION_ID at dispatch (see direct_session_run). None otherwise.
    origin_session_id: str | None = None

    def __post_init__(self) -> None:
        if self.profile not in VALID_PROFILES:
            raise ValueError(
                f"Invalid profile {self.profile!r}. "
                f"Must be one of: {', '.join(sorted(VALID_PROFILES))}"
            )
        # Frozen dataclass: derive the delivery mode from the legacy bools when
        # not explicitly set, so every existing constructor keeps its behavior
        # without an edit. object.__setattr__ is required under frozen=True.
        if self.delivery_mode is None:
            object.__setattr__(
                self,
                "delivery_mode",
                DeliveryMode.from_legacy(self.notify, self.notify_on_failure_only),
            )


@dataclass
class DirectSessionResult:
    """What the background session produced."""

    session_id: str
    cc_session_id: str = ""
    success: bool = False
    output_text: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    tools_called: list[dict] = field(default_factory=list)
    model_used: str = ""
    roster_model: str = ""  # roster NAME the chokepoint selected ("glm-5.2"/"claude")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class DirectSessionRunner:
    """Spawns profile-constrained background CC sessions.

    Parameters
    ----------
    invoker:
        A *dedicated* CCInvoker instance (not the shared runtime one).
        This avoids the ``_active_proc`` concurrency race under
        ``Semaphore(2)`` (architect finding #5).
    session_manager:
        Shared SessionManager for tracking.
    config_builder:
        Builds system prompts and MCP configs.
    runtime:
        GenesisRuntime reference — used to lazily access
        ``outreach_pipeline`` (which isn't available at init time
        because outreach bootstraps after cc_relay).
    """

    _MAX_CONCURRENT = 2

    def __init__(
        self,
        *,
        invoker: AgentProvider,
        session_manager: SessionManager,
        config_builder: SessionConfigBuilder,
        runtime: object,
    ) -> None:
        self._invoker = invoker
        self._session_manager = session_manager
        self._config_builder = config_builder
        self._rt = runtime
        self._semaphore = asyncio.Semaphore(self._MAX_CONCURRENT)
        self._active: dict[str, asyncio.Task] = {}
        self._protected_paths: object | None = None
        self._auditor: object | None = None

    def set_protected_paths(self, registry: object) -> None:
        """Inject ProtectedPathRegistry for background session prompt hardening."""
        self._protected_paths = registry

    def set_auditor(self, auditor: object) -> None:
        """Inject PostExecutionAuditor for post-session autonomy feedback."""
        self._auditor = auditor

    # -- Public API --------------------------------------------------------

    async def spawn(self, request: DirectSessionRequest) -> str:
        """Fire-and-forget. Returns genesis session_id immediately.

        Includes a lightweight autonomy ceiling check: if the background
        cognitive category has been fully regressed, block ALL background
        spawns as a circuit breaker. This is defense-in-depth — the
        proposal gate handles fine-grained domain classification.
        """
        # Ceiling check: skip for foreground/user-initiated sessions.
        # NOTE: DirectSessionRequest.source_tag defaults to "direct_session",
        # so we intentionally exclude it from the skip set — only explicitly
        # foreground or user-initiated sessions bypass the check.
        _SKIP_TAGS = {"foreground", "user_request"}
        if request.source_tag not in _SKIP_TAGS:
            mgr = getattr(self._rt, "_autonomy_manager", None)
            if mgr is not None:
                try:
                    from genesis.autonomy.types import AutonomyCategory

                    state = await mgr.get_state(
                        AutonomyCategory.BACKGROUND_COGNITIVE.value,
                    )
                    # Block if corrections have fully regressed trust
                    # (posterior < 0.15 means overwhelming corrections)
                    if state is not None:
                        from genesis.db.crud.autonomy import bayesian_posterior

                        posterior = bayesian_posterior(
                            state.total_successes,
                            state.total_corrections,
                        )
                        if posterior < 0.15 and state.total_corrections > 3:
                            logger.warning(
                                "Spawn blocked: background_cognitive posterior %.3f "
                                "(L%d, %dS/%dC) — autonomy circuit breaker",
                                posterior,
                                state.current_level,
                                state.total_successes,
                                state.total_corrections,
                            )
                            raise RuntimeError(
                                f"Autonomy circuit breaker: background_cognitive "
                                f"posterior {posterior:.3f} below threshold"
                            )
                except RuntimeError:
                    raise  # re-raise the circuit breaker
                except Exception:
                    # Non-fatal: if check fails, allow spawn
                    logger.debug(
                        "Autonomy ceiling check failed (non-fatal)",
                        exc_info=True,
                    )

        session = await self._session_manager.create_background(
            session_type=SessionType.BACKGROUND_TASK,
            model=request.model,
            effort=request.effort,
            source_tag=request.source_tag,
            dispatch_mode="direct",
            profile=request.profile,
            # WS-3: same per-profile provenance the CCInvocation env stamp
            # uses (_build_invocation) — now durable in cc_sessions.
            origin=_PROFILE_ORIGIN.get(request.profile),
            # Record the skills resolved for this session so the
            # skill-evolution effectiveness analyzer has usage signal.
            # (Same resolution used for prompt injection in _build_invocation.)
            skill_tags=_resolve_skills(request),
        )
        session_id = session["id"]

        task = tracked_task(
            self._run_session(request, session_id),
            name=f"direct-session-{session_id[:8]}",
        )
        self._active[session_id] = task
        task.add_done_callback(lambda _t: self._active.pop(session_id, None))
        return session_id

    def active_count(self) -> int:
        return len(self._active)

    async def shutdown(self, *, grace_s: float = 10.0) -> int:
        """Cancel in-flight session tasks and await their handlers.

        Called by ``GenesisRuntime.shutdown()`` BEFORE the DB closes so the
        CancelledError handler in ``_run_session`` can persist a terminal
        'failed' status. Without this, a ``systemctl restart`` tears the
        event loop down only after the DB is closed — the handler's writes
        no-op and the rows linger 'active' until the stale reaper.

        ``grace_s`` bounds the wait because shutdown runs under systemd's
        TimeoutStopSec (~90s) hard deadline: an unbounded wait on a wedged
        CC child would push the whole unit into SIGKILL, losing every later
        cleanup step (including the DB close itself). 10s is ample for the
        handler's few DB writes.

        Returns the number of tasks that were still in flight.
        """
        tasks = [t for t in self._active.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=grace_s)
        return len(tasks)

    @staticmethod
    def _summarize_tools(tools_called: list[dict]) -> dict[str, int]:
        """Aggregate tool calls into {name: count} dict."""
        counts: dict[str, int] = {}
        for t in tools_called[:100]:
            name = t.get("name", "unknown")
            counts[name] = counts.get(name, 0) + 1
        return counts

    # -- Internal ----------------------------------------------------------

    async def _run_session(
        self,
        request: DirectSessionRequest,
        session_id: str,
    ) -> DirectSessionResult:
        """Execute a single CC session. Called inside tracked_task."""
        _set_obs_session(session_id)
        telemetry: list[dict] = []
        start = time.monotonic()

        async def on_event(event: StreamEvent) -> None:
            if event.event_type == "tool_use" and event.tool_name:
                telemetry.append(
                    {
                        "name": event.tool_name,
                        "input_preview": (str(event.tool_input)[:200] if event.tool_input else ""),
                    }
                )

        try:
            async with self._semaphore:
                invocation = self._build_invocation(request, session_id)
                # Create this session's isolated CC sandbox (off cc-tmp) just
                # before the run; removed in the finally below. The guard is
                # intentional: if claude_code_tmpdir is unset (tests, or any
                # future non-isolated path), CC falls back to the shared cc-tmp
                # (the old behaviour) rather than this crashing.
                if invocation.claude_code_tmpdir:
                    Path(invocation.claude_code_tmpdir).mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                output: CCOutput = await self._invoker.run_streaming(
                    invocation,
                    on_event=on_event,
                )

            elapsed = time.monotonic() - start
            result = DirectSessionResult(
                session_id=session_id,
                cc_session_id=output.session_id,
                success=not output.is_error,
                output_text=(
                    output.text + _BG_TRUNCATION_NOTICE if output.bg_truncated else output.text
                ),
                error=output.error_message if output.is_error else None,
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                duration_s=round(elapsed, 1),
                tools_called=telemetry,
                model_used=output.model_used,
                roster_model=output.roster_model,
            )

            # Persist result in session metadata (merge, don't overwrite)
            await self._store_result(session_id, request, result)

            # Turn-independent fallback recovery: a successful run on the HOME model
            # proves it's back (no foreground conversation turn needed). "Home" is the
            # rate-limited model recorded at failover (state.original) — which may be a
            # roster PEER when the configured default is non-Claude, NOT necessarily
            # native Claude. A success on any OTHER model (incl. an intentional native
            # pin while a peer is the down home) must NOT clear the fallback.
            if result.success:
                from genesis.cc import fallback_state
                from genesis.cc.fallback_recovery import note_home_recovery

                st = fallback_state.read()
                if st.is_fallback and result.roster_model == (st.original or roster.CLAUDE):
                    await note_home_recovery()

            # Feed outcome back to ego proposal if this was a proposal dispatch
            await self._record_proposal_outcome(request, result)

            # Post-execution audit: verify protected paths, feed autonomy signals.
            # Only for ego dispatches (caller_context starts with "ego_proposal:").
            # Runs inline (cheap) — transcript parsing is I/O-bound but fast.
            if (
                self._auditor is not None
                and request.caller_context
                and request.caller_context.startswith("ego_proposal:")
            ):
                try:
                    metadata = {}
                    db = getattr(self._rt, "_db", None)
                    if db is not None:
                        from genesis.db.crud import cc_sessions as cs_crud

                        row = await cs_crud.get_by_id(db, session_id)
                        if row and row.get("metadata"):
                            with contextlib.suppress(json.JSONDecodeError, TypeError):
                                metadata = json.loads(row["metadata"])

                    await self._auditor.audit_session(
                        session_id,
                        transcript_path=metadata.get("transcript_path", ""),
                        tools_summary=metadata.get("tools_summary"),
                        session_success=result.success,
                        caller_context=request.caller_context,
                    )
                except Exception:
                    logger.debug(
                        "Post-execution audit failed for %s (non-fatal)",
                        session_id[:8],
                        exc_info=True,
                    )

            await self._session_manager.complete(
                session_id,
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

            # RESULT-mode: deliver the finished outcome back to the origin
            # conversation. Best-effort (own try/except) — never fails the run.
            if request.delivery_mode == DeliveryMode.RESULT:
                await self._deliver_result_to_origin(request, result)

            # If this session was a rate-limit resume, close its park now that the
            # result is delivered (no-op unless caller_context carries a park id).
            _db = getattr(self._rt, "_db", None)
            if _db is not None:
                await rate_limit_park.mark_resumed_if_lineage(_db, request.caller_context)

            logger.info(
                "Direct session %s completed: %.1fs, $%.4f, %d tools",
                session_id[:8],
                elapsed,
                output.cost_usd,
                len(telemetry),
            )
            return result

        except asyncio.CancelledError:
            # CancelledError is a BaseException — the Exception handler
            # below never sees it, so a cancelled session used to linger
            # 'active' until the stale reaper swept it. A cancel (runtime
            # shutdown via self.shutdown(), task.cancel) is a KNOWN
            # interruption: record it as failed. Best-effort — cancellation
            # was already delivered at the await point above, so these writes
            # normally complete; a closed DB is caught and logged, while a
            # genuinely re-delivered second cancel propagates immediately
            # (skipping the log — same terminal task state either way).
            elapsed = time.monotonic() - start
            cancel_result = DirectSessionResult(
                session_id=session_id,
                success=False,
                error="CancelledError: session cancelled",
                duration_s=round(elapsed, 1),
                tools_called=telemetry,
            )
            try:
                await self._store_result(session_id, request, cancel_result)
                # Feed the outcome back to an ego proposal, matching the
                # generic failure path below.
                await self._record_proposal_outcome(request, cancel_result)
                await self._session_manager.fail(
                    session_id,
                    reason="cancelled",
                )
            except Exception:
                logger.error(
                    "Failed to record session %s cancellation",
                    session_id[:8],
                    exc_info=True,
                )
            logger.warning(
                "Direct session %s cancelled after %.1fs",
                session_id[:8],
                elapsed,
            )
            raise

        except (CCRateLimitError, CCQuotaExhaustedError) as exc:
            # A rate/quota limit is NOT a real failure — park the work durably so
            # it auto-resumes when capacity returns, instead of the misleading
            # "FAILED" alert the generic handler emits. If this session was itself
            # a resume, park_direct_session re-limits its own park in place
            # (preserving the attempts/escalation lineage). mode=off or no db →
            # fall through to the generic failure path (current behavior).
            db = getattr(self._rt, "_db", None)
            park_id = None
            if db is not None:
                park_id = await rate_limit_park.park_direct_session(db, request=request, exc=exc)
            if park_id is not None:
                elapsed = time.monotonic() - start
                parked_result = DirectSessionResult(
                    session_id=session_id,
                    success=False,
                    error=f"rate_limited: parked for resume ({park_id})",
                    duration_s=round(elapsed, 1),
                    tools_called=telemetry,
                )
                try:
                    await self._store_result(session_id, request, parked_result)
                    await self._session_manager.fail(
                        session_id,
                        reason="rate_limited_parked",
                    )
                except Exception:
                    logger.error(
                        "Failed to record parked session %s",
                        session_id[:8],
                        exc_info=True,
                    )
                logger.info(
                    "Direct session %s rate-limited → parked %s for resume",
                    session_id[:8],
                    park_id,
                )
                return parked_result
            # Parking disabled/failed — treat as a normal failure (re-raises).
            await self._finalize_failure(session_id, request, exc, telemetry, start)

        except Exception as exc:
            await self._finalize_failure(session_id, request, exc, telemetry, start)

        finally:
            # Remove this session's isolated CC sandbox (created off cc-tmp just
            # before the run above). Best-effort; the disk-hygiene reaper catches
            # any orphans left by a hard SIGKILL that skips this finally.
            shutil.rmtree(_bg_session_root(session_id), ignore_errors=True)

    async def _finalize_failure(
        self,
        session_id: str,
        request: DirectSessionRequest,
        exc: BaseException,
        telemetry: list[dict],
        start: float,
    ) -> None:
        """Record + deliver a genuine session failure, then re-raise.

        Extracted from the generic ``except`` so the rate-limit catch can reuse
        the exact same path when parking is disabled (mode=off). Ends with
        ``raise`` to preserve the original propagate-after-record contract.
        """
        elapsed = time.monotonic() - start
        error_result = DirectSessionResult(
            session_id=session_id,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_s=round(elapsed, 1),
            tools_called=telemetry,
        )

        # Best-effort: persist failure and notify
        try:
            await self._store_result(session_id, request, error_result)
            await self._record_proposal_outcome(request, error_result)
            await self._session_manager.fail(
                session_id,
                reason=str(exc)[:500],
            )
        except Exception:
            logger.error(
                "Failed to record session %s failure",
                session_id[:8],
                exc_info=True,
            )

        # RESULT-mode delivers the failure to the origin thread (a promised
        # report that failed is still owed to the requester); otherwise the
        # legacy broadcast failure alert fires. _deliver_result_to_origin
        # swallows its own errors, so no extra guard is needed here.
        if request.delivery_mode == DeliveryMode.RESULT:
            await self._deliver_result_to_origin(request, error_result)
        elif request.notify:
            try:
                await self._notify(request, error_result, success=False)
            except Exception:
                logger.error(
                    "Failed to send failure notification for %s",
                    session_id[:8],
                    exc_info=True,
                )

        logger.error(
            "Direct session %s failed after %.1fs: %s",
            session_id[:8],
            elapsed,
            exc,
        )
        raise

    async def _record_proposal_outcome(
        self,
        request: DirectSessionRequest,
        result: DirectSessionResult,
    ) -> None:
        """Feed session outcome back to ego proposal for feedback loop."""
        if not request.caller_context or not request.caller_context.startswith("ego_proposal:"):
            return
        proposal_id = request.caller_context.split(":", 1)[1]
        advisories: list[str] = []
        try:
            from genesis.db.crud.ego import (
                mark_proposal_verification_failed,
                update_proposal_outcome,
            )

            db = getattr(self._rt, "_db", None)
            if db is None:
                return

            # Post-dispatch verification: if the session succeeded and the
            # proposal defines expected_outputs, check deliverables. Missing or
            # empty files are hard failures; size/content misses are advisories
            # that KEEP the proposal executed — a real deliverable was produced,
            # and a string/size miss is a failure of the proxy, not the work.
            if result.success:
                vres = await self._verify_proposal_outputs(db, proposal_id)
                if vres is not None and vres.missing_files:
                    # Deliverable not produced → hard fail + observation.
                    fail_summary = "; ".join(vres.missing_files)
                    await mark_proposal_verification_failed(
                        db,
                        proposal_id,
                        summary=fail_summary,
                    )
                    try:
                        store = getattr(self._rt, "_memory_store", None)
                        if store is not None:
                            await store.store(
                                content=(
                                    f"Ego dispatch VERIFICATION FAILED for "
                                    f"proposal {proposal_id}: {fail_summary}"
                                ),
                                source="ego_dispatch_verification",
                                tags=["ego", "verification_failure"],
                                memory_type="episodic",
                                wing="autonomy",
                                room="ego",
                                source_subsystem="ego",
                            )
                    except Exception:
                        logger.debug(
                            "Failed to store verification observation",
                            exc_info=True,
                        )
                    await self._notify_dispatch_debrief(
                        proposal_id,
                        request,
                        result,
                    )
                    return
                if vres is not None:
                    advisories = list(vres.advisories)

            base = result.output_text or result.error or ""
            if advisories:
                # Informational only — never changes the |completed: polarity
                # the feedback harvester reads from this suffix. Budget the note
                # into the 1000-char cap so a long output_text can't truncate
                # the advisory away entirely (the whole point of surfacing it).
                note = f"\n[verification advisories: {'; '.join(advisories)}]"[:500]
                summary = base[: max(0, 1000 - len(note))] + note
            else:
                summary = base[:1000]
            await update_proposal_outcome(
                db,
                proposal_id,
                success=result.success,
                summary=summary,
            )
            # On failure: create observation so ego sees it next cycle
            if not result.success:
                try:
                    store = getattr(self._rt, "_memory_store", None)
                    if store is not None:
                        await store.store(
                            content=(f"Ego dispatch FAILED for proposal {proposal_id}: {summary}"),
                            source="ego_dispatch_outcome",
                            tags=["ego", "dispatch_failure"],
                            memory_type="episodic",
                            wing="autonomy",
                            room="ego",
                            source_subsystem="ego",
                        )
                except Exception:
                    logger.debug("Failed to store failure observation", exc_info=True)
        except Exception:
            logger.warning(
                "Failed to record proposal outcome for %s",
                proposal_id,
                exc_info=True,
            )
        # Debrief is best-effort, fully self-contained (own try/except)
        await self._notify_dispatch_debrief(proposal_id, request, result, advisories=advisories)

    async def _verify_proposal_outputs(
        self,
        db: object,
        proposal_id: str,
    ) -> VerificationResult | None:
        """Run post-dispatch verification for a completed proposal.

        Returns the :class:`~genesis.ego.verification.VerificationResult`
        (inspect ``missing_files`` for hard failures and ``advisories`` for
        non-fatal size/content notes), or ``None`` if the proposal has no
        ``expected_outputs`` or verification could not run.
        """
        try:
            from genesis.db.crud.ego import get_proposal
            from genesis.ego.verification import parse_expected_outputs, verify_outputs

            proposal = await get_proposal(db, proposal_id)
            if not proposal:
                return None
            expected = parse_expected_outputs(proposal.get("expected_outputs"))
            if expected is None:
                return None  # no verification configured
            return verify_outputs(expected)
        except Exception:
            logger.warning(
                "Post-dispatch verification error for %s (skipping)",
                proposal_id,
                exc_info=True,
            )
            return None

    async def _notify_dispatch_debrief(
        self,
        proposal_id: str,
        request: DirectSessionRequest,
        result: DirectSessionResult,
        advisories: list[str] | None = None,
    ) -> None:
        """Send after-action report to ego_dispatches Telegram topic.

        ``advisories`` are non-fatal verification notes (a content/size proxy
        that didn't match a still-accepted deliverable); surfaced as an FYI,
        never as a failure.
        """
        try:
            import html as html_mod

            pw = getattr(self._rt, "_ego_proposal_workflow", None)
            tm = getattr(pw, "_topic_manager", None) if pw else None
            if tm is None:
                return

            status = "✓ Completed" if result.success else "✗ Failed"
            outcome = (result.output_text or result.error or "no output")[:400]
            outcome_escaped = html_mod.escape(outcome)

            cost_str = f"${result.cost_usd:.4f}" if result.cost_usd else "—"
            dur_str = f"{result.duration_s:.0f}s" if result.duration_s else "—"

            msg = (
                f"<b>Dispatch Debrief</b> [{status}]\n"
                f"<i>Proposal:</i> {proposal_id[:12]}\n"
                f"<i>Duration:</i> {dur_str} | <i>Cost:</i> {cost_str}\n\n"
                f"<b>Outcome:</b>\n{outcome_escaped}"
            )
            if advisories:
                adv_escaped = html_mod.escape("; ".join(advisories))
                msg += f"\n\n<i>Advisory (deliverable accepted):</i>\n{adv_escaped}"
            await tm.send_to_category("ego_dispatches", msg)
        except Exception:
            logger.debug("Failed to send dispatch debrief", exc_info=True)

    def _build_invocation(
        self,
        request: DirectSessionRequest,
        session_id: str,
    ) -> CCInvocation:
        system_prompt = request.system_prompt
        if system_prompt is None:
            # Use the surplus config's system prompt (which loads SOUL.md)
            surplus_config = self._config_builder.build_surplus_config()
            system_prompt = surplus_config.get("system_prompt", "")

        # Inject profile addendum (tells session its constraints upfront)
        system_prompt += _build_profile_addendum(request.profile)

        # Inject protected paths into background session prompt (Layer 2 defense).
        # Foreground sessions get this via CCInvoker; background sessions were
        # missing it. This makes the LLM aware of path restrictions even when
        # tool_exceptions grant Write access.
        if self._protected_paths is not None:
            format_fn = getattr(self._protected_paths, "format_for_prompt", None)
            if format_fn:
                protection_context = format_fn()
                if protection_context:
                    system_prompt += "\n\n" + protection_context

        # Inject skills (explicit from request, auto-detected from prompt keywords)
        skill_names = _resolve_skills(request)
        if skill_names:
            from genesis.learning.skills.wiring import load_skill

            for name in skill_names:
                content = load_skill(name)
                if content:
                    system_prompt += f"\n\n## Skill: {name}\n{content}"

        disallowed = list(PROFILES.get(request.profile, PROFILES["observe"]))

        # Per-request tool exceptions: remove specific tools from the disallow
        # list so the session can use them.  Used for scoped jobs that need
        # narrow file-write or shell access (e.g., models.md weekly synthesis).
        if request.tool_exceptions:
            exceptions = set(request.tool_exceptions)
            # Never let a background session re-enable recursive spawn via a
            # tool_exception: the delivery model's foreground-origin-only
            # invariant (GENESIS_SESSION_ID is always a foreground row) depends
            # on direct_session_run staying unreachable from background sessions.
            exceptions.discard("mcp__genesis-health__direct_session_run")
            disallowed = [t for t in disallowed if t not in exceptions]

        # Give background sessions access to Genesis MCP servers. Profile
        # determines which servers (see module-level _PROFILE_TO_MCP, which
        # profile overlays may extend): campaign/interact get outreach,
        # observe/research get health + memory only.
        mcp_profile = _PROFILE_TO_MCP.get(request.profile, "reflection")
        mcp_config = self._config_builder.build_mcp_config(profile=mcp_profile)

        # Prepend planning instruction if the caller opted in.
        prompt = request.prompt
        if request.planning_instruction:
            prompt = f"{request.planning_instruction}\n\n{prompt}"

        # Interact profile pins to Opus — browser reasoning + ATS anti-bot
        # detection demand high capability. This both UPGRADES weaker models
        # (haiku/sonnet → opus) and intentionally PINS DOWN Fable to Opus: Fable
        # is not yet evaluated on the browser/ATS path (see the "separate eval"
        # note in docs/reference/cc-compatibility.md). Flip this to a tier floor
        # if/when Fable is cleared for interact work.
        model = request.model
        if request.profile == "interact" and model != CCModel.OPUS:
            logger.info("interact profile: pinning model to opus (requested %s)", model)
            model = CCModel.OPUS

        # Intentional per-dispatch model SELECTION. When a roster_model is named,
        # pin it: stamp its overrides and set roster_eligible only when ROUTED, so
        # the chokepoint honors the endpoint with correct attribution (routed peer)
        # or runs native without re-selecting the global default (claude). With no
        # roster_model, roster_eligible=True lets the chokepoint apply the global
        # active model as before. overrides_for raises RosterError for an
        # unknown/keyless model → propagates to _run_session → recorded as a FAILED
        # result (fail loud; never silently run the default for an explicit ask).
        routing: dict = {}
        roster_eligible = True
        if request.roster_model is not None:
            routing = roster.overrides_for(request.roster_model)
            roster_eligible = bool(routing)
            # interact forces Opus for browser/ATS reasoning; a routed roster_model
            # overrides the endpoint and defeats that guarantee — surface it loudly
            # rather than silently running the peer model for a capability-sensitive
            # profile.
            if routing and request.profile == "interact":
                logger.warning(
                    "interact profile dispatched with roster_model=%s — running on "
                    "the peer model instead of Opus; browser/ATS reasoning may degrade",
                    request.roster_model,
                )

        return CCInvocation(
            prompt=prompt,
            model=model,
            effort=request.effort,
            system_prompt=system_prompt,
            append_system_prompt=True,
            timeout_s=request.timeout_s,
            # Background lane may legitimately own long dispatched Workflow work
            # (e.g. deep-research, 100+ agents). Let the CLI wait for bg tasks up
            # to the full budget instead of the default 600s truncation; the
            # invoker clamps this below timeout_s so the graceful CLI truncation
            # + partial flush always precedes the hard SIGKILL.
            bg_wait_ceiling_ms=request.timeout_s * 1000,
            skip_permissions=True,
            disallowed_tools=disallowed,
            working_dir=background_session_dir(),
            # WS-3: per-profile session provenance (see _PROFILE_ORIGIN).
            origin=_PROFILE_ORIGIN.get(request.profile),
            claude_code_tmpdir=_bg_session_sandbox(session_id),
            mcp_config=mcp_config,
            bash_allowlist=_PROFILE_BASH_ALLOWLIST.get(request.profile, ()),
            roster_eligible=roster_eligible,
            **routing,
        )

    async def _store_result(
        self,
        session_id: str,
        request: DirectSessionRequest,
        result: DirectSessionResult,
    ) -> None:
        """Merge result data into cc_sessions.metadata (read-merge-write)."""
        from genesis.db.crud import cc_sessions

        db = getattr(self._rt, "_db", None)
        if db is None:
            return

        row = await cc_sessions.get_by_id(db, session_id)
        existing = {}
        if row and row.get("metadata"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                loaded = json.loads(row["metadata"])
                # Guard non-dict JSON roots (array/str/number) so existing.update
                # below can't AttributeError on a malformed/foreign metadata blob.
                if isinstance(loaded, dict):
                    existing = loaded

        tool_counts = self._summarize_tools(result.tools_called)

        # Derive transcript path from CC's project-key convention:
        # working_dir ~/.genesis/background-sessions → project key
        # -home-ubuntu--genesis-background-sessions → transcript .jsonl.
        # NOTE: CC encodes EVERY non-alphanumeric char (incl. the leading
        # dot of ~/.genesis) as '-', so use cc_project_key — a bare
        # .replace("/", "-") leaves the dot and yields a wrong path.
        transcript_path = ""
        if result.cc_session_id:
            project_key = cc_project_key(background_session_dir())
            transcript_path = str(
                Path.home() / ".claude" / "projects" / project_key / f"{result.cc_session_id}.jsonl"
            )

        existing.update(
            {
                "profile": request.profile,
                "caller_context": request.caller_context,
                "output_text": result.output_text[:20000],
                "tools_summary": tool_counts,
                "cc_session_id": result.cc_session_id,
                "transcript_path": transcript_path,
                "error": result.error,
                "model_used": result.model_used,
                "duration_s": result.duration_s,
            }
        )

        # Roster resume continuity: record the endpoint a routed session ran on,
        # keyed off the roster model NAME the chokepoint selected (ground truth),
        # so a future resume can target the same provider. No-op for native Claude.
        # Token never stored (NAME only).
        if result.roster_model and result.roster_model != roster.CLAUDE:
            payload = roster.endpoint_payload(result.roster_model)
            if payload:
                existing["roster_endpoint"] = payload

        await db.execute(
            "UPDATE cc_sessions SET metadata = ? WHERE id = ?",
            (json.dumps(existing), session_id),
        )
        await db.commit()

    async def _notify(
        self,
        request: DirectSessionRequest,
        result: DirectSessionResult,
        *,
        success: bool,
    ) -> None:
        """Send Telegram notification via outreach pipeline.

        Only failures are sent — successes are logged to the DB and visible
        via direct_session_list MCP tool and the dashboard.  Sending success
        notifications as ALERT was noise: "Direct Session Complete" is not
        an alert, and in DM-only installs it drowns real alerts.
        """
        if success:
            return

        pipeline = getattr(self._rt, "_outreach_pipeline", None)
        if pipeline is None:
            logger.debug("Outreach pipeline not available, skipping notification")
            return

        tool_counts = self._summarize_tools(result.tools_called)
        tools_str = (
            ", ".join(
                f"{n} ({c})"
                for n, c in sorted(
                    tool_counts.items(),
                    key=lambda x: -x[1],
                )[:8]
            )
            or "none"
        )

        body = (
            f"<b>Direct Session FAILED</b>\n\n"
            f"Profile: {request.profile} | "
            f"Model: {result.model_used or request.model} | "
            f"Duration: {result.duration_s:.0f}s\n\n"
            f"Error: {result.error or 'unknown'}\n\n"
            f"Tools before failure: {tools_str}"
        )

        try:
            from genesis.outreach.types import OutreachCategory, OutreachRequest

            await pipeline.submit(
                OutreachRequest(
                    category=OutreachCategory.ALERT,
                    topic="direct_session_fail",
                    context=body,
                    salience_score=0.9,
                    verbatim=True,  # composed failure detail — never reword
                )
            )
        except Exception:
            logger.error("Outreach submit failed", exc_info=True)

    @staticmethod
    def _resolve_origin_target(
        channel: str | None,
        chat_id: str | None,
        user_id: str,
        thread_id_raw: object,
        forum_chat_id: str | None,
    ) -> tuple[str | None, int | None]:
        """Resolve a Telegram ``(chat_id, message_thread_id)`` delivery target
        from an origin ``cc_sessions`` row.

        Prefers the persisted origin ``chat_id`` (captured at intake) — correct
        for a DM, a group, AND a forum topic uniformly (a forum topic's
        ``chat.id`` *is* the supergroup). Falls back to best-effort
        reconstruction for legacy rows written before ``chat_id`` was captured.
        Returns ``(None, None)`` when the origin cannot be addressed. The
        addressable-channel test is shared with the reroute nudge via
        ``origin_delivery_supported`` (single source of truth).
        """
        if not origin_delivery_supported(channel):
            return None, None
        tid: int | None = None
        if thread_id_raw:
            try:
                tid = int(thread_id_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None, None
        # Preferred: the real origin chat id. Handles DM / group / forum topic.
        if chat_id:
            return str(chat_id), tid
        # --- Legacy fallback (rows predating chat_id capture) ---
        # Forum-topic origin without a persisted chat: the supergroup, if set.
        if tid is not None:
            return (str(forum_chat_id), tid) if forum_chat_id else (None, None)
        # DM origin: the chat id equals the Telegram user id (``tg-<id>``).
        # NOTE: a legacy no-thread GROUP origin is indistinguishable from a DM
        # here and would resolve to the user's DM — the exact gap the persisted
        # chat_id closes for all new sessions.
        if user_id.startswith("tg-"):
            num = user_id[3:]
            if num.lstrip("-").isdigit():
                return num, None
        return None, None

    async def _deliver_result_to_origin(
        self,
        request: DirectSessionRequest,
        result: DirectSessionResult,
    ) -> None:
        """Deliver a RESULT-mode session's terminal outcome to its origin thread.

        Routes the finished output (success) or the error (failure/truncation)
        back to the exact conversation the session was dispatched from — the
        channel + thread on the origin foreground ``cc_sessions`` row
        (``request.origin_session_id``). Best-effort: any failure is logged, never
        raised, so a delivery problem cannot fail the session handler. Oversized
        output is written to ``~/.genesis/output`` and delivered as a head +
        pointer. Delivered via ``submit_urgent(verbatim=True)`` so a promised
        result is never suppressed by governance/dedup or reworded by the drafter.
        """
        origin_id = request.origin_session_id
        if not origin_id:
            logger.warning(
                "RESULT delivery for session %s has no origin_session_id — skipping",
                result.session_id[:8],
            )
            return
        try:
            import html as html_mod

            pipeline = getattr(self._rt, "_outreach_pipeline", None)
            db = getattr(self._rt, "_db", None)
            if pipeline is None or db is None:
                logger.warning("RESULT delivery: outreach pipeline or DB unavailable")
                return

            from genesis.db.crud import cc_sessions as cs_crud

            origin = await cs_crud.get_by_id(db, origin_id) or {}
            if not origin:
                logger.warning(
                    "RESULT delivery: origin session %s not found — using fallback surface",
                    origin_id[:8],
                )

            chat_id, thread_id = self._resolve_origin_target(
                origin.get("channel"),
                origin.get("chat_id"),
                origin.get("user_id") or "",
                origin.get("thread_id"),
                getattr(pipeline, "_forum_chat_id", None),
            )

            status = "✓" if result.success else "✗"
            raw = (
                (result.output_text or "(no output)")
                if result.success
                else f"Task did not complete: {result.error or 'unknown error'}"
            )
            header = f"<b>{status} Background task complete</b>\n\n"
            escaped = html_mod.escape(raw)
            text = header + escaped
            if len(text) > _TG_MSG_CAP:
                path = _write_result_artifact(result.session_id, raw)
                # Budget on the ESCAPED length (entities expand up to ~4×), then
                # trim any dangling partial entity at the cut so Telegram's HTML
                # parser doesn't choke on a truncated "&amp;"-style sequence.
                head = escaped[: _TG_MSG_CAP - 400]
                if "&" in head[-6:]:
                    head = head.rsplit("&", 1)[0]
                pointer = html_mod.escape(str(path)) if path else "(disk write failed)"
                text = header + head + f"\n\n… (truncated) — full result saved to {pointer}"

            if not chat_id:
                # Origin unaddressable (non-telegram, or a chat we don't persist):
                # deliver to the default owner surface rather than drop the
                # promised result silently.
                text += (
                    "\n\n<i>(origin conversation could not be addressed — "
                    "delivered to the default surface)</i>"
                )

            from genesis.outreach.types import OutreachCategory, OutreachRequest

            await pipeline.submit_urgent(
                OutreachRequest(
                    category=OutreachCategory.ALERT,
                    # Unique per session so governance dedup never collides with
                    # another delivery.
                    topic=f"bg_result:{result.session_id}",
                    context=text,
                    salience_score=0.9,
                    channel="telegram" if chat_id else None,
                    verbatim=True,
                    target_chat_id=chat_id,
                    target_thread_id=thread_id,
                )
            )
        except Exception:
            logger.error(
                "Failed to deliver RESULT to origin for session %s",
                result.session_id[:8],
                exc_info=True,
            )
