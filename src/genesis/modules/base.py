"""Base protocol and optional base class for capability modules."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CapabilityModule(Protocol):
    """A pluggable external capability for Genesis.

    Modules are external domain capabilities (crypto trading, prediction markets,
    prospecting, etc.) that leverage Genesis's cognitive services without modifying
    core. They are "hands, not brain" — they can be plugged in and unplugged
    without affecting Genesis identity, reflection, or learning.

    ## Implementing a Native Module

    Two patterns:

    **Pattern A — Manual (existing style, all 4 built-in modules):**
    Implement every protocol method directly. Override ``configurable_fields()``
    to return dicts with keys: name, label, type, value, description.
    Optionally include: default, min, max, required, sensitive, options.

    **Pattern B — ModuleBase mixin (new modules):**
    Inherit ``ModuleBase`` and declare class-level attributes::

        class MyModule(ModuleBase):
            __module_meta__ = {
                "name": "my_module",
                "display_name": "My Module",
                "description": "What it does",
                "category": "custom",
                "tags": ["tag1"],
                "version": "1.0.0",
                "enabled": False,
                "research_profile": None,
            }
            __module_config_fields__ = [
                ConfigField("threshold", "float", "Signal Threshold",
                            description="Min signal strength", default=0.5,
                            min=0.0, max=1.0),
            ]

    ``__module_meta__`` also enables **auto-discovery**: the bootstrap loader
    scans ``genesis/modules/*/module.py`` and registers any class with this
    attribute without requiring a YAML file. YAML configs (if present) always
    take precedence.

    ## Config Field Schema

    ``configurable_fields()`` returns ``list[dict]``. Supported keys:

    - **name** (required): snake_case field key
    - **label** (required): human-readable label for the UI
    - **type** (required): ``"str"`` | ``"int"`` | ``"float"`` | ``"bool"`` |
      ``"enum"`` | ``"secret"`` | ``"list"``
    - **value** (required): live current value
    - **description**: tooltip / help text
    - **default**: value shown as placeholder / used on reset
    - **min** / **max**: numeric bounds for int/float fields
    - **required**: if True, empty value is rejected before update_config()
    - **sensitive**: if True, value is masked in UI (use ``"secret"`` type instead
      when the field IS a credential; use ``sensitive: True`` for less sensitive
      cases where masking is still desired)
    - **options**: list of ``{"value": ..., "label": "..."}`` dicts (enum type only)

    The dashboard uses these keys to render the correct widget and run
    client-side pre-validation. The PATCH endpoint also validates ``min``,
    ``max``, and ``required`` before calling ``update_config()``.
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

        See class docstring for the full supported key schema.
        Default implementation (in ModuleBase mixin): reads __module_config_fields__.
        """
        ...

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply configuration updates and return the new config state.

        The PATCH endpoint pre-validates min/max/required before calling this.
        Modules should still validate cross-field constraints here.
        Default: no-op, returns empty dict.
        """
        ...


class ModuleBase:
    """Optional base class for native capability modules.

    Provides default implementations of ``configurable_fields()`` and
    ``update_config()`` driven by the ``__module_config_fields__`` class
    attribute. Inherit this when you want zero boilerplate for simple
    field declarations.

    Example::

        from genesis.modules.base import ModuleBase
        from genesis.modules.config_schema import ConfigField

        class MyModule(ModuleBase):
            __module_meta__ = {"name": "my_module", "enabled": False}
            __module_config_fields__ = [
                ConfigField("interval_s", "int", "Check Interval (s)",
                            description="Seconds between checks",
                            default=60, min=10),
            ]

            def __init__(self):
                self._interval_s = 60
                self._enabled = False

            @property
            def name(self) -> str:
                return "my_module"

            @property
            def enabled(self) -> bool:
                return self._enabled

            @enabled.setter
            def enabled(self, value: bool) -> None:
                self._enabled = value

            # register(), deregister(), etc. implemented as needed

    The default ``configurable_fields()`` reads live values from ``self._<name>``
    instance attributes. Override the method if your live values live elsewhere.
    """

    def configurable_fields(self) -> list[dict]:
        """Return config fields from __module_config_fields__ with live values."""
        fields = getattr(self.__class__, "__module_config_fields__", [])
        result = []
        for f in fields:
            value = getattr(self, f"_{f.name}", f.default)
            result.append(f.to_dict(value=value))
        return result

    def update_config(self, updates: dict) -> dict:
        """Apply updates to __module_config_fields__ via _<name> instance attrs.

        The framework PATCH endpoint pre-validates min/max/required before this
        is called. Override for cross-field validation or non-standard storage.
        """
        fields = getattr(self.__class__, "__module_config_fields__", [])
        field_map = {f.name: f for f in fields}
        for key, value in updates.items():
            if key not in field_map:
                continue
            f = field_map[key]
            # Type coercion
            if f.type == "int":
                value = int(value)
            elif f.type == "float":
                value = float(value)
            elif f.type == "bool":
                # bool("false") == True, so normalize string representations
                if isinstance(value, str):
                    value = value.lower() in ("true", "1", "yes", "on")
                else:
                    value = bool(value)
            setattr(self, f"_{key}", value)
        return {d["name"]: d["value"] for d in self.configurable_fields()}
