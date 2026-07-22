"""Types for Claude Code integration."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class SessionType(StrEnum):
    FOREGROUND = "foreground"
    BACKGROUND_REFLECTION = "background_reflection"
    BACKGROUND_TASK = "background_task"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class MessageType(StrEnum):
    QUESTION = "question"
    DECISION = "decision"
    ERROR = "error"
    FINDING = "finding"
    COMPLETION = "completion"
    PROGRESS = "progress"


class MessagePriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MessageSource(StrEnum):
    CC_FOREGROUND = "cc_foreground"
    CC_BACKGROUND = "cc_background"
    AZ = "az"
    USER = "user"


class ChannelType(StrEnum):
    TERMINAL = "terminal"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    WEB = "web"
    VOICE = "voice"


class CCModel(StrEnum):
    SONNET = "sonnet"
    OPUS = "opus"
    HAIKU = "haiku"
    FABLE = "fable"  # top tier, above Opus (claude-fable-5)

    @staticmethod
    def from_full_name(full_name: str) -> CCModel | None:
        """Map a full model identifier to its CCModel tier.

        Examples: "claude-opus-4-8" -> OPUS, "claude-sonnet-5" -> SONNET,
        "claude-fable-5" -> FABLE.
        Returns None if the full name doesn't match any known tier.
        Assumes each model name contains exactly one tier keyword (Anthropic
        naming convention). First substring match wins.
        """
        lower = full_name.lower()
        for member in CCModel:
            if member.value in lower:
                return member
        return None


class EffortLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class DeliveryMode(StrEnum):
    """How a background (direct) session's terminal outcome is delivered.

    - ``SILENT`` — no notification; the DB row is the only record.
    - ``FAILURE_ONLY`` — failures are broadcast-alerted; success is silent.
      The legacy default (success notifications were retired as noise).
    - ``RESULT`` — the terminal outcome (success AND failure/truncation) is
      delivered back to the ORIGIN conversation the session was dispatched
      from. Requires ``DirectSessionRequest.origin_session_id`` (the foreground
      ``cc_sessions`` row id captured at dispatch); falls back to the default
      owner surface when the origin cannot be addressed.
    """

    SILENT = "silent"
    FAILURE_ONLY = "failure_only"
    RESULT = "result"

    @classmethod
    def from_legacy(cls, notify: bool, notify_on_failure_only: bool = False) -> DeliveryMode:
        """Map the legacy ``notify`` / ``notify_on_failure_only`` bools to a mode.

        Preserves today's behavior exactly: ``notify=False`` → SILENT; any
        ``notify=True`` → FAILURE_ONLY (success has never notified — the runner
        only sends on failure). ``notify_on_failure_only`` is accepted for
        signature completeness but does not change the result: nothing in the
        runner reads it today, so both ``notify=True`` combinations collapse to
        FAILURE_ONLY. No legacy caller maps to RESULT — that mode is opt-in via
        the ``deliver_to_origin`` dispatch path only.
        """
        if not notify:
            return cls.SILENT
        return cls.FAILURE_ONLY


# Ordered list used for ceiling comparisons (low → max).
_EFFORT_RANK: list[EffortLevel] = [
    EffortLevel.LOW,
    EffortLevel.MEDIUM,
    EffortLevel.HIGH,
    EffortLevel.XHIGH,
    EffortLevel.MAX,
]

# Maximum effort tier supported by each CC model. Opus, Sonnet, and Fable all
# accept the full low..max range (incl. xhigh/max) — verified live against the
# claude CLI on 2026-07-02: `sonnet` → claude-sonnet-5, `fable` → claude-fable-5,
# `opus` → claude-opus-4-8, each accepted `--effort xhigh` and `--effort max`.
# Haiku (claude-haiku-4-5) is intentionally ABSENT: it does not use an effort
# setting at all (the CLI tolerates the flag but it is a no-op), so callers must
# OMIT --effort for Haiku rather than pass a wasted value — gate on
# model_supports_effort() below.
_MODEL_EFFORT_CEILING: dict[CCModel, EffortLevel] = {
    CCModel.OPUS: EffortLevel.MAX,
    CCModel.SONNET: EffortLevel.MAX,
    CCModel.FABLE: EffortLevel.MAX,
}


def model_supports_effort(model: CCModel) -> bool:
    """Whether *model* uses the ``--effort`` flag at all.

    Haiku does not use an effort setting; the claude CLI tolerates the flag but
    it is a no-op, so every ``claude -p`` call site MUST omit ``--effort`` for
    Haiku rather than pass a wasted value. All other tiers accept the full
    low..max range.
    """
    return model in _MODEL_EFFORT_CEILING


def model_name_supports_effort(model_name: str) -> bool:
    """Whether a model *string* (tier alias or full id) uses the ``--effort`` flag.

    Resolves the string to a tier via :meth:`CCModel.from_full_name`; an
    unrecognized name (e.g. a roster/provider id) is treated as effort-capable
    (``True``) so a model we cannot classify isn't silently stripped of effort.
    Wraps ``from_full_name`` + :func:`model_supports_effort` so call sites that
    hold a model STRING (Guardian, remote dispatch) don't each re-derive it.
    """
    tier = CCModel.from_full_name(model_name)
    return tier is None or model_supports_effort(tier)


def clamp_effort(model: CCModel, effort: EffortLevel) -> EffortLevel:
    """Return *effort* clamped to the maximum supported by *model*.

    Opus/Sonnet/Fable accept the full low..max range (verified live against the
    claude CLI, 2026-07-02), so this is currently a no-op for them — it remains
    as a hook for any future weaker tier that caps below ``max``. Models with no
    effort support (Haiku) have no ceiling entry and are returned unchanged;
    gate emission with :func:`model_supports_effort` instead of relying on this.
    Returns the (possibly clamped) effort; caller should warn on mismatch.
    """
    ceiling = _MODEL_EFFORT_CEILING.get(model)
    if ceiling is None:
        return effort
    if _EFFORT_RANK.index(effort) > _EFFORT_RANK.index(ceiling):
        return ceiling
    return effort


#: Canonical sets of selectable CC model-tier / effort names, derived from the
#: enums. EVERY ``claude -p`` model/effort validator across the codebase MUST
#: derive its allowed set from these (never a hardcoded ``{"opus","sonnet",...}``
#: literal) so a new tier (e.g. ``fable``) or effort level is accepted at every
#: selection surface at once. Enforced by tests/test_cc/test_effort_model_coverage.py.
VALID_MODEL_NAMES: frozenset[str] = frozenset(m.value for m in CCModel)
VALID_EFFORT_NAMES: frozenset[str] = frozenset(e.value for e in EffortLevel)


@dataclass(frozen=True)
class CCInvocation:
    prompt: str
    model: CCModel = CCModel.SONNET
    effort: EffortLevel = EffortLevel.MEDIUM
    system_prompt: str | None = None
    resume_session_id: str | None = None
    output_format: str = "json"
    mcp_config: str | None = None
    # 2h (7200s) project floor. A short default silently guillotines legitimate
    # long CC work — the conversational path timed out at 600s on 2026-06-30
    # mid-task. Per the genesis-dev timeout policy, caps on cognitive/CC paths
    # fight Genesis; the subprocess kill still bounds a truly-hung process. Call
    # sites that MUST fail fast set an explicit shorter value (e.g. the CC
    # fallback liveness probe uses 300s).
    timeout_s: int = 7200
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    skip_permissions: bool = False
    skill_tags: list[str] | None = None
    working_dir: str | None = None
    # Per-invocation override for CC's Bash sandbox root (CLAUDE_CODE_TMPDIR).
    # None → the shared default (~/.genesis/cc-tmp). Set by throwaway sessions
    # (e.g. the model-roster gauntlet) to isolate their tmp blast radius from
    # live sessions policed by genesis-tmp-watchgod.
    claude_code_tmpdir: str | None = None
    # When non-empty, the session's Bash is restricted to these command binaries
    # (enforced by scripts/bash_safety_hook.sh via the GENESIS_BASH_ALLOWLIST env
    # var). Used by Bash-enabled background profiles (e.g. "steward") to scope
    # shell access to a single tool (gh) without granting an open shell.
    bash_allowlist: tuple[str, ...] = ()
    bare: bool = False
    # --safe-mode: start with all customizations (CLAUDE.md, skills, plugins,
    # hooks, MCP servers, custom commands/agents) disabled, with OAuth intact —
    # unlike --bare, which refuses OAuth and requires ANTHROPIC_API_KEY. Used by
    # the eval bench's "bare Claude" arm: safe-mode is the only OAuth-compatible
    # way to suppress the user-level CLAUDE.md, which CC discovers via the
    # passwd-resolved home directory regardless of $HOME/$CLAUDE_CONFIG_DIR
    # (probe-verified 2026-07-09). Built-in tools remain available.
    safe_mode: bool = False
    # --strict-mcp-config: CC honors ONLY the servers in --mcp-config, ignoring
    # user/project-scope MCP configs. Without it, mcp_config is additive.
    strict_mcp_config: bool = False
    append_system_prompt: bool = False
    stream_idle_timeout_ms: int | None = None
    # Headless CC (-p) waits for dispatched background Workflow/subagent tasks
    # to finish before emitting the final result, capped by the CLI's
    # CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS (default 600_000ms = 10min, v2.1.182+).
    # At the cap the CLI SIGKILLs the whole tree and flushes a PARTIAL result —
    # this silently truncated a 100+-agent deep-research run on 2026-07-20. Set
    # this (ms) to own the ceiling for lanes that legitimately run long background
    # work (e.g. direct_session). The invoker clamps it to stay strictly below
    # timeout_s so the CLI's graceful truncation always beats the hard SIGKILL,
    # and an operator's inherited env var wins (set via env, not this field).
    # None → CLI default (600s) stands; correct for foreground turns, which must
    # never linger (long work is routed to the background lane instead).
    bg_wait_ceiling_ms: int | None = None
    anthropic_base_url: str | None = None  # Proxy URL override (ANTHROPIC_BASE_URL)
    # Model-roster routing (model diversification). When set, the CC subprocess
    # is pointed at a non-Anthropic provider via its native Anthropic-compatible
    # endpoint: anthropic_auth_token → ANTHROPIC_AUTH_TOKEN; model_id_override →
    # the provider's model id via ANTHROPIC_MODEL (NOT --model, which the CLI
    # would let win over the env var). Resolved by the roster policy layer
    # (genesis.cc.roster.apply_active) at the CCInvoker chokepoint; the invoker
    # only honors these fields, never selects.
    # anthropic_auth_token is repr=False: it holds a live provider token at
    # runtime, so it must never surface in an accidental log/repr of the invocation.
    anthropic_auth_token: str | None = field(default=None, repr=False)
    model_id_override: str | None = None
    # Opt-in to roster routing. apply_active() (at the invoker chokepoint) is a
    # no-op for invocations with roster_eligible=False — so only the surfaces that
    # opt in (foreground conversation, background DirectSession) are routed; every
    # other CC call site stays Claude-native until a dedicated activation pass.
    roster_eligible: bool = False
    # Silent-cap detection opt-in. When True, the invoker fires its
    # on_cc_empty_output callback if this invocation returns genuinely-empty
    # output (no text, no error, no rate_limit_event) — the signature of a
    # silent Anthropic-subscription cap that otherwise reads as a "successful"
    # empty completion. Default False = zero behavior change; only output-
    # producing COGNITIVE call sites (ego, reflection, weekly jobs, sentinel,
    # autonomy executors, mail judge) opt in. NEVER changes control flow —
    # detection/alerting only, never a raise or failover.
    expect_output: bool = False
    # cc-loop-01: opaque per-session key for the invoker's proc registry, so an
    # interrupt (e.g. Telegram /stop) targets THIS session's subprocess and not
    # a concurrent background one. None → keyed by pid (never cross-fired).
    session_key: str | None = None
    # WS-3 session-level provenance. When set, CCInvoker._build_env stamps
    # GENESIS_SESSION_ORIGIN so the session's memory MCP writes carry this
    # origin_class (memory.provenance.session_origin_from_env). Set it ONLY at
    # dispatch sites whose sessions process external content by construction
    # (inbox eval, mail judge, research, external-facing DirectSession
    # profiles). None → env var popped → writes classify first_party via
    # pipeline derivation. Validated LOUDLY in __post_init__ (a typo'd origin
    # silently degrading to first_party would resurrect the exact origin-loss
    # gap this field closes).
    origin: str | None = None
    # WS-3 B4 gate-4: True ONLY for owner-attended interactive conversations
    # (terminal/telegram ConversationManager). CCInvoker._build_env stamps
    # GENESIS_SESSION_SUPERVISED so immunity_shadow.is_dispatched_session_env
    # excludes these from the pushed-surfaces enforce drop — GENESIS_SESSION_ID
    # alone is an ATTRIBUTION id (foreground conversations set it too, via
    # observability.session_context), not a supervision signal. Default False:
    # headless/background dispatches are unsupervised; a new foreground path
    # that forgets this flag fails toward dropping wrapped-external pushed
    # content there (visible in the enforce ledger + auto-demote), never
    # toward injecting into an unsupervised session.
    supervised: bool = False
    # Applied LAST in CCInvoker._build_env, after every computed key — the
    # per-invocation escape hatch for env the invoker doesn't model (e.g. the
    # eval bench's CLAUDE_CONFIG_DIR cleanroom). Overrides win over inherited
    # os.environ AND the invoker's own settings; use deliberately. repr=False:
    # values may reference credential paths.
    env_overrides: dict[str, str] | None = field(default=None, repr=False)
    on_spawn: Callable[[int], Awaitable[None]] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        # Producer-side loud validation of the WS-3 origin (the env READER is
        # fail-safe instead): a dispatch-site typo like "external-untrusted"
        # must fail at construction, not silently classify a session's memory
        # writes first_party. Deferred import keeps cc.types light.
        if self.origin is not None:
            from genesis.memory.provenance import ORIGIN_CLASSES

            if self.origin not in ORIGIN_CLASSES:
                raise ValueError(
                    f"CCInvocation.origin={self.origin!r} is not a valid "
                    f"origin_class (expected one of {sorted(ORIGIN_CLASSES)})"
                )


# Background CC session isolation.  Background sessions run from a
# directory OUTSIDE the project tree so Claude Code's resume picker
# (which prefix-matches project dirs when worktrees exist) doesn't
# include them in the foreground session list.
_BACKGROUND_SESSION_DIR = Path.home() / ".genesis" / "background-sessions"


def background_session_dir() -> str:
    """Absolute path for background CC session working directory.

    Creates the directory if it doesn't exist.  Uses ``~/.genesis/``
    (outside the repo tree) so CC's worktree-aware resume picker does
    not match it against the main project prefix.
    """
    _BACKGROUND_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    return str(_BACKGROUND_SESSION_DIR)


def cc_project_key(working_dir: str) -> str:
    """Claude Code's project-key encoding for a working-directory path.

    CC names each project's transcript directory under
    ``~/.claude/projects/`` by replacing every non-alphanumeric character
    in the absolute path with ``-`` (consecutive separators are NOT
    collapsed).  e.g. ``/home/u/.genesis/background-sessions`` →
    ``-home-u--genesis-background-sessions`` (the ``/.`` becomes ``--``).

    Replicating the FULL encoding (not just ``/`` → ``-``) matters because
    the background-session dir is ``~/.genesis/...``: the leading dot must
    be encoded too, or the derived transcript path is wrong and downstream
    readers (audit, bookmark enrichment) silently miss the transcript.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", working_dir)


@dataclass(frozen=True)
class CCOutput:
    session_id: str
    text: str
    model_used: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    duration_ms: int
    exit_code: int
    is_error: bool = False
    error_message: str | None = None
    model_requested: str = ""
    downgraded: bool = False
    via_proxy: bool = False
    # The roster model NAME selected at the chokepoint (genesis.cc.roster) — e.g.
    # "claude" (native) or "glm-5.2". Ground truth for what we ROUTED to (set from
    # apply_active), independent of the provider's self-reported model_used, which
    # may be a variant string or empty. Used for resume-endpoint persistence.
    roster_model: str = ""
    # True when the CLI hit its background-task wait ceiling and SIGKILLed
    # dispatched Workflow/subagent work mid-run, flushing only a PARTIAL result
    # (detected from the "Background tasks still running after …; terminating"
    # stderr marker). Callers surface this — a visible truncation notice to the
    # user and/or a cc.bg_truncated observability event — so the silent-death
    # class (2026-07-20 deep-research) can never recur unremarked.
    bg_truncated: bool = False


@dataclass(frozen=True)
class StreamEvent:
    """A single event from CC's stream-json output."""

    event_type: str  # "init", "text", "thinking", "tool_use", "tool_result", "result", "system", "system_notice"
    text: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    session_id: str | None = None
    raw: dict | None = None

    @classmethod
    def from_raw(cls, raw: dict) -> StreamEvent:
        etype = raw.get("type", "")

        if etype == "assistant":
            content = raw.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "thinking":
                    return cls(
                        event_type="thinking",
                        text=block.get("thinking"),
                        raw=raw,
                    )
                if block.get("type") == "text":
                    return cls(event_type="text", text=block.get("text"), raw=raw)
                if block.get("type") == "tool_use":
                    return cls(
                        event_type="tool_use",
                        tool_name=block.get("name"),
                        tool_input=block.get("input"),
                        raw=raw,
                    )
            return cls(event_type="assistant", raw=raw)

        if etype == "user":
            # tool_result events
            return cls(event_type="tool_result", raw=raw)

        if etype == "result":
            return cls(
                event_type="result",
                session_id=raw.get("session_id"),
                text=raw.get("result"),
                raw=raw,
            )

        if etype == "system" and raw.get("subtype") == "init":
            return cls(
                event_type="init",
                session_id=raw.get("session_id"),
                raw=raw,
            )

        return cls(event_type=etype, raw=raw)


@dataclass(frozen=True)
class IntentResult:
    raw_text: str = ""
    model_override: CCModel | None = None
    effort_override: EffortLevel | None = None
    resume_requested: bool = False
    resume_session_id: str | None = None
    task_requested: bool = False
    cleaned_text: str = ""
    intent_only: bool = False
