"""Types for Claude Code integration."""

from __future__ import annotations

from dataclasses import dataclass
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
    MAX = "max"


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
    bare: bool = False
    append_system_prompt: bool = False
    stream_idle_timeout_ms: int | None = None


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
