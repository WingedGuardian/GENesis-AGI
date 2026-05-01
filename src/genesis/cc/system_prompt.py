"""System prompt assembler for foreground CC sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from genesis.db.crud import cognitive_state


class SystemPromptAssembler:
    """Assembles the Genesis system prompt from identity files + cognitive state."""

    def __init__(self, *, identity_dir: Path | None = None):
        if identity_dir is None:
            identity_dir = Path(__file__).resolve().parent.parent / "identity"
        self._dir = identity_dir

    def _read(self, name: str) -> str | None:
        path = self._dir / name
        if path.exists():
            return path.read_text().strip()
        return None

    def assemble_static(self) -> str:
        """Assemble the static portions (no DB needed)."""
        parts: list[str] = []

        soul = self._read("SOUL.md")
        if soul:
            parts.append(soul)

        voice = self._read("VOICE.md")
        if voice:
            parts.append(voice)

        user = self._read("USER.md")
        if user:
            parts.append(user)

        parts.append(f"Date: {datetime.now(UTC).strftime('%Y-%m-%d')}")

        conversation = self._read("CONVERSATION.md")
        if conversation:
            parts.append(conversation)

        session_history = self._read("SESSION_HISTORY.md")
        if session_history:
            parts.append(session_history)

        steering = self._read("STEERING.md")
        if steering:
            parts.append(steering)

        # Layer 2: Protected paths awareness for relay sessions
        protection_block = self._build_protection_block()
        if protection_block:
            parts.append(protection_block)

        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _build_protection_block() -> str | None:
        """Load protected paths and format for system prompt injection."""
        try:
            from genesis.autonomy.protection import ProtectedPathRegistry

            registry = ProtectedPathRegistry.from_yaml()
            return registry.format_for_prompt()
        except Exception:
            return None

    async def assemble(
        self,
        *,
        db,
        model: str | None = None,
        effort: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Assemble full prompt including cognitive state from DB."""
        parts: list[str] = []

        # Runtime config goes FIRST so the model internalizes it before
        # identity/conversation content.  This lets it answer meta-questions
        # like "what model are you?" or "what's your thinking effort?"
        if model or effort or session_id:
            meta_lines = ["## Your Session Configuration"]
            meta_lines.append(
                "You KNOW the following about your own session. "
                "When asked, state these facts directly."
            )
            if model:
                meta_lines.append(f"- Model: {model}")
            if effort:
                meta_lines.append(f"- Thinking effort: {effort}")
            if session_id:
                meta_lines.append(f"- Session ID: {session_id}")
            parts.append("\n".join(meta_lines))

        parts.append(self.assemble_static())

        cog = await cognitive_state.render(db)
        if cog:
            parts.append("## Current Cognitive State\n\n" + cog)

        return "\n\n---\n\n".join(parts)
