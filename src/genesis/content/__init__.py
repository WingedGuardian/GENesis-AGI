"""genesis.content — platform-aware content formatting and drafting."""

from genesis.content.drafter import ContentDrafter
from genesis.content.formatter import ContentFormatter
from genesis.content.limits import get_limits
from genesis.content.types import (
    DraftRequest,
    DraftResult,
    FormatTarget,
    FormattedContent,
    PlatformLimits,
)

__all__ = [
    "ContentDrafter",
    "ContentFormatter",
    "DraftRequest",
    "DraftResult",
    "FormatTarget",
    "FormattedContent",
    "PlatformLimits",
    "get_limits",
]
