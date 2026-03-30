"""Markdown → Telegram HTML converter.

Converts CC's markdown output to Telegram-compatible HTML. Scope is
intentionally narrow — only handles patterns CC actually produces
(measured from 6300 responses in Batch 0D analysis):

  inline_code: 41%, bold: 26%, unordered_lists: 11%, numbered_lists: 10%,
  tables: 5%, code_blocks: 3%, ampersand: 2%, angle_brackets: 2%,
  blockquotes: 1%, headers: 1%, links: 0.3%, strikethrough: ~0%

Safety property: if this produces invalid HTML, callers catch BadRequest
and fall back to plain text via safe_html(). Formatting loss is acceptable;
delivery failure is not.
"""

from __future__ import annotations

import html
import re

# Patterns used to identify and replace markdown constructs.
# Order matters — code blocks must be extracted BEFORE inline processing.

# Fenced code blocks: ```lang\n...\n```
_CODE_BLOCK_RE = re.compile(
    r"```(?:\w+)?\n(.*?)```",
    re.DOTALL,
)

# Inline code: `code`
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# Bold: **text** or __text__
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")

# Italic: *text* (but not **text**)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")

# Strikethrough: ~~text~~
_STRIKE_RE = re.compile(r"~~(.+?)~~")

# Links: [text](url) — supports one level of balanced parentheses in URLs
_LINK_RE = re.compile(r"\[([^\]]+)\]\(((?:[^()]*\([^()]*\))*[^()]*)\)")

# Allowed URL schemes for links (block javascript: etc.)
_SAFE_SCHEMES = frozenset({"http", "https", "mailto", "tg"})

# Headers: # Header (converted to bold — Telegram has no header tag)
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def safe_html(text: str) -> str:
    """Escape text for safe inclusion in Telegram HTML. No formatting."""
    return html.escape(text)


def md_to_telegram_html(text: str) -> str:
    """Convert markdown text to Telegram-compatible HTML.

    Handles: code blocks, inline code, bold, italic, strikethrough,
    links, headers. Escapes HTML special characters in non-code text.

    Returns valid Telegram HTML or best-effort approximation. Callers
    should catch BadRequest and fall back to safe_html().
    """
    if not text:
        return text

    # Step 1: Extract code blocks into placeholders (before any escaping)
    code_blocks: list[str] = []

    def _replace_code_block(m: re.Match) -> str:
        idx = len(code_blocks)
        # Escape HTML inside code blocks
        code_blocks.append(html.escape(m.group(1).rstrip()))
        return f"\x00CODEBLOCK{idx}\x00"

    result = _CODE_BLOCK_RE.sub(_replace_code_block, text)

    # Handle unclosed code blocks (CC response truncated mid-block)
    if '```' in result:
        _unclosed_re = re.compile(r"```(?:\w+)?\n?(.*)$", re.DOTALL)
        def _replace_unclosed(m: re.Match) -> str:
            idx = len(code_blocks)
            code_blocks.append(html.escape(m.group(1).rstrip()))
            return f"\x00CODEBLOCK{idx}\x00"
        result = _unclosed_re.sub(_replace_unclosed, result)

    # Step 2: Extract inline code into placeholders
    inline_codes: list[str] = []

    def _replace_inline_code(m: re.Match) -> str:
        idx = len(inline_codes)
        inline_codes.append(html.escape(m.group(1)))
        return f"\x00INLINE{idx}\x00"

    result = _INLINE_CODE_RE.sub(_replace_inline_code, result)

    # Step 3: Escape HTML special chars in remaining text
    # But preserve our placeholders (they contain \x00 which html.escape ignores)
    result = html.escape(result)

    # Step 4: Apply inline formatting
    result = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", result)
    result = _ITALIC_RE.sub(r"<i>\1</i>", result)
    result = _STRIKE_RE.sub(r"<s>\1</s>", result)
    def _safe_link(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme and scheme not in _SAFE_SCHEMES:
            return text  # Strip dangerous links, keep text
        return f'<a href="{url.replace(chr(34), "%22")}">{text}</a>'

    result = _LINK_RE.sub(_safe_link, result)
    result = _HEADER_RE.sub(lambda m: f"<b>{m.group(2)}</b>", result)

    # Step 5: Restore code blocks and inline code
    for idx, code in enumerate(code_blocks):
        result = result.replace(f"\x00CODEBLOCK{idx}\x00", f"<pre>{code}</pre>")

    for idx, code in enumerate(inline_codes):
        result = result.replace(f"\x00INLINE{idx}\x00", f"<code>{code}</code>")

    return result
