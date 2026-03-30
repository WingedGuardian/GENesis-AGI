"""Content formatting and drafting type definitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FormatTarget(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    TERMINAL = "terminal"
    GENERIC = "generic"


@dataclass(frozen=True)
class PlatformLimits:
    max_length: int
    supports_markdown: bool = True
    supports_html: bool = False
    supports_code_blocks: bool = True
    truncation_suffix: str = "..."


@dataclass(frozen=True)
class FormattedContent:
    text: str
    target: FormatTarget
    truncated: bool = False
    original_length: int = 0


@dataclass(frozen=True)
class DraftRequest:
    topic: str
    context: str = ""
    target: FormatTarget = FormatTarget.GENERIC
    tone: str = "professional"
    max_length: int | None = None
    system_prompt: str | None = None


@dataclass(frozen=True)
class DraftResult:
    content: FormattedContent
    model_used: str = ""
    raw_draft: str = ""
