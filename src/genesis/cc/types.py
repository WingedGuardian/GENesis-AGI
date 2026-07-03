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
    append_system_prompt: bool = False
    stream_idle_timeout_ms: int | None = None
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
    # cc-loop-01: opaque per-session key for the invoker's proc registry, so an
    # interrupt (e.g. Telegram /stop) targets THIS session's subprocess and not
    # a concurrent background one. None → keyed by pid (never cross-fired).
    session_key: str | None = None
    on_spawn: Callable[[int], Awaitable[None]] | None = field(
        default=None, compare=False, repr=False,
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
