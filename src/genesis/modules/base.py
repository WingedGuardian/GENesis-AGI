"""Base protocol for capability modules."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CapabilityModule(Protocol):
    """A pluggable external capability for Genesis.

    Modules are external domain capabilities (crypto trading, prediction markets,
    prospecting, etc.) that leverage Genesis's cognitive services without modifying
    core. They are "hands, not brain" — they can be plugged in and unplugged
    without affecting Genesis identity, reflection, or learning.
    """

    @property
    def name(self) -> str:
        """Unique module identifier."""
        ...

    @property
    def enabled(self) -> bool:
        """Whether this module is currently active."""
        ...

    async def register(self, runtime: Any) -> None:
        """Register with Genesis runtime — subscribe to pipeline, initialize."""
        ...

    async def deregister(self) -> None:
        """Clean shutdown. Remove pipeline subscription, stop tracking."""
        ...

    def get_research_profile_name(self) -> str | None:
        """Return the research profile name for Knowledge Pipeline subscription.

        None if this module doesn't use the pipeline.
        """
        ...

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        """Process a surfaced opportunity. Returns action proposal for user approval,
        or None if not actionable."""
        ...

    async def record_outcome(self, outcome: dict) -> None:
        """Record domain-specific outcome in isolated tracking."""
        ...

    async def extract_generalizable(self, outcome: dict) -> list[dict] | None:
        """LLM pass: extract lessons generalizable beyond this domain.

        Returns observations suitable for Genesis core memory, or None if
        nothing is generalizable.
        """
        ...

    def configurable_fields(self) -> list[dict[str, Any]]:
        """Return list of user-editable configuration fields.

        Each dict has: name, label, type (str/int/float/bool), value, description.
        Optional if the module has no user-configurable params.
        Default: returns empty list.
        """
        ...

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply configuration updates and return the new config state.

        Optional. Default: no-op, returns empty dict.
        """
        ...
