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

    @staticmethod
    def from_full_name(full_name: str) -> CCModel | None:
        """Map a full model identifier to its CCModel tier.

        Examples: "claude-opus-4-6" -> OPUS, "claude-sonnet-4-6" -> SONNET.
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

# Maximum effort tier supported by each CC model.
# XHIGH and MAX are Opus-only; the API silently accepts them on
# Sonnet/Haiku (no error, just wasted/meaningless). Guard at dispatch
# so intent is always visible in logs.
_MODEL_EFFORT_CEILING: dict[CCModel, EffortLevel] = {
    CCModel.OPUS: EffortLevel.MAX,
    CCModel.SONNET: EffortLevel.HIGH,
    CCModel.HAIKU: EffortLevel.HIGH,
}


def clamp_effort(model: CCModel, effort: EffortLevel) -> EffortLevel:
    """Return *effort* clamped to the maximum supported by *model*.

    XHIGH and MAX are Opus-only.  Sonnet/Haiku ceiling is HIGH.
    Returns the (possibly clamped) effort; caller should warn on mismatch.
    """
    ceiling = _MODEL_EFFORT_CEILING.get(model, EffortLevel.HIGH)
    if _EFFORT_RANK.index(effort) > _EFFORT_RANK.index(ceiling):
        return ceiling
    return effort


@dataclass(frozen=True)
class CCInvocation:
    prompt: str
    model: CCModel = CCModel.SONNET
    effort: EffortLevel = EffortLevel.MEDIUM
    system_prompt: str | None = None
    resume_session_id: str | None = None
    output_format: str = "json"
    mcp_config: str | None = None
    timeout_s: int = 600
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    skip_permissions: bool = False
    skill_tags: list[str] | None = None
    working_dir: str | None = None
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
