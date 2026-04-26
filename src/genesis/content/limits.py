"""Platform-specific content limits."""

from genesis.content.types import FormatTarget, PlatformLimits

PLATFORM_LIMITS: dict[FormatTarget, PlatformLimits] = {
    FormatTarget.TELEGRAM: PlatformLimits(
        max_length=4096,
        supports_markdown=True,
        supports_html=True,
        supports_code_blocks=True,
    ),
    FormatTarget.TWITTER: PlatformLimits(
        max_length=280,
        supports_markdown=False,
        supports_html=False,
        supports_code_blocks=False,
        truncation_suffix="...",
    ),
    FormatTarget.LINKEDIN: PlatformLimits(
        max_length=3000,
        supports_markdown=False,
        supports_html=False,
        supports_code_blocks=False,
    ),
    FormatTarget.MEDIUM: PlatformLimits(
        max_length=100_000,
        supports_markdown=True,
        supports_html=True,
        supports_code_blocks=True,
    ),
    FormatTarget.EMAIL: PlatformLimits(
        max_length=50_000,
        supports_markdown=True,
        supports_html=True,
        supports_code_blocks=True,
    ),
    FormatTarget.TERMINAL: PlatformLimits(
        max_length=100_000,
        supports_markdown=True,
        supports_html=False,
        supports_code_blocks=True,
    ),
    FormatTarget.GENERIC: PlatformLimits(
        max_length=100_000,
        supports_markdown=True,
        supports_html=False,
        supports_code_blocks=True,
    ),
}


def get_limits(target: FormatTarget) -> PlatformLimits:
    """Get platform limits for a target, with generic fallback."""
    return PLATFORM_LIMITS.get(target, PLATFORM_LIMITS[FormatTarget.GENERIC])
