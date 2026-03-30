"""ResponseFormatter — formats CC output for different channels."""

from __future__ import annotations

from genesis.cc.types import ChannelType

_TELEGRAM_MAX = 4096
_WHATSAPP_MAX = 4096


class ResponseFormatter:
    def format(self, text: str, *, channel: ChannelType) -> list[str]:
        if channel in (ChannelType.TERMINAL, ChannelType.WEB):
            return [text]
        max_len = _TELEGRAM_MAX if channel == ChannelType.TELEGRAM else _WHATSAPP_MAX
        return self._split_preserving_code(text, max_len)

    def format_question(self, content: dict, *, channel: ChannelType) -> list[str]:
        text = content.get("text", "")
        options = content.get("options", [])
        if options:
            text += "\n\n" + "\n".join(
                f"{i + 1}. {opt}" for i, opt in enumerate(options)
            )
        context = content.get("context")
        if context:
            text = f"[{context}]\n\n{text}"
        return self.format(text, channel=channel)

    def _split_preserving_code(self, text: str, max_len: int) -> list[str]:
        """Split text into chunks ≤ max_len, avoiding breaks inside code blocks.

        If a chunk boundary falls inside an open code block, backtrack to
        before the code block started and split there. If the code block
        itself exceeds max_len, force-split it (with balanced fences).
        """
        if len(text) <= max_len:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= max_len:
                chunks.append(remaining)
                break

            # Find a safe split point
            split_at = max_len
            candidate = remaining[:split_at]

            # Check if we're inside an unclosed code block
            fence_count = candidate.count("```")
            if fence_count % 2 != 0:
                # We're inside a code block — try to backtrack to before it
                last_fence = candidate.rfind("```")
                if last_fence > 0:
                    # Check for a newline before the fence for a cleaner split
                    newline_before = candidate.rfind("\n", 0, last_fence)
                    if newline_before > max_len // 4:
                        split_at = newline_before
                    else:
                        # Code block is too large; force-split and add closing fence
                        chunk = remaining[:max_len - 4]  # leave room for \n```
                        chunk += "\n```"
                        chunks.append(chunk)
                        remaining = "```\n" + remaining[max_len - 4:]
                        continue
                else:
                    split_at = max_len

            # Prefer splitting at a newline near the split point
            candidate = remaining[:split_at]
            newline_pos = candidate.rfind("\n", max(0, split_at - 200), split_at)
            if newline_pos > 0:
                split_at = newline_pos + 1  # include the newline

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        return chunks
