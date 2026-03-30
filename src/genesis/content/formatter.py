"""ContentFormatter — pure synchronous platform-aware text formatting."""

from __future__ import annotations

import re

from genesis.content.limits import get_limits
from genesis.content.types import FormatTarget, FormattedContent


class ContentFormatter:
    """Pure, synchronous formatter. No dependencies, no I/O."""

    def format(self, text: str, target: FormatTarget) -> FormattedContent:
        """Format text for a platform, truncating if needed."""
        limits = get_limits(target)
        original_length = len(text)

        if not limits.supports_markdown:
            text = strip_markdown(text)

        truncated = len(text) > limits.max_length
        if truncated:
            cut_at = limits.max_length - len(limits.truncation_suffix)
            text = text[:cut_at] + limits.truncation_suffix

        return FormattedContent(
            text=text,
            target=target,
            truncated=truncated,
            original_length=original_length,
        )

    def split_long(
        self, text: str, target: FormatTarget
    ) -> list[FormattedContent]:
        """Split text into multiple messages for the platform.

        Splits at paragraph boundaries first, then sentence boundaries.
        """
        limits = get_limits(target)
        max_len = limits.max_length

        if len(text) <= max_len:
            return [FormattedContent(text=text, target=target, original_length=len(text))]

        chunks: list[str] = []
        paragraphs = text.split("\n\n")
        current = ""

        for para in paragraphs:
            candidate = f"{current}\n\n{para}".strip() if current else para
            if len(candidate) <= max_len:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # If a single paragraph exceeds max_len, split by sentences
                if len(para) > max_len:
                    chunks.extend(_split_by_sentence(para, max_len))
                else:
                    current = para
                    continue
                current = ""

        if current:
            chunks.append(current)

        return [
            FormattedContent(text=c, target=target, original_length=len(text))
            for c in chunks
        ]


def strip_markdown(text: str) -> str:
    """Remove common markdown formatting."""
    # Bold/italic
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}(.*?)_{1,3}", r"\1", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Headers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text


def _split_by_sentence(text: str, max_len: int) -> list[str]:
    """Split text by sentence boundaries to fit within max_len."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for s in sentences:
        candidate = f"{current} {s}".strip() if current else s
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single sentence exceeds, hard-truncate
            if len(s) > max_len:
                chunks.append(s[:max_len])
            else:
                current = s
                continue
            current = ""
    if current:
        chunks.append(current)
    return chunks
