"""Typed configuration field schema for Genesis capability modules.

Provides ConfigField — a descriptor that carries full metadata for a
user-editable module configuration parameter. Used by:

- Native modules: returned from configurable_fields() (enriched dicts)
- External modules: parsed from YAML config_fields section
- Dashboard: determines which input widget to render
- PATCH endpoint: pre-validates bounds before delegating to update_config()
- ModuleBase mixin: drives default configurable_fields() implementation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Sentinel: distinguishes "no value provided" from value=None in to_dict()
_MISSING = object()

FieldType = Literal["str", "int", "float", "bool", "enum", "secret", "list"]


@dataclass
class EnumOption:
    """One selectable option for an enum-type ConfigField."""

    value: Any
    label: str

    @classmethod
    def from_dict(cls, data: dict) -> EnumOption:
        return cls(value=data["value"], label=data.get("label", str(data["value"])))

    def to_dict(self) -> dict:
        return {"value": self.value, "label": self.label}


@dataclass
class ConfigField:
    """Typed descriptor for a single user-editable module configuration field.

    This is the single source of truth for field metadata. It drives:
    - Dashboard input widget selection (type → widget)
    - Server-side constraint pre-validation (min, max, required)
    - Client-side validation hints (min, max, placeholder from default)
    - Sensitive field masking (secret type / sensitive flag)

    Usage in native modules::

        # Option A — declare class-level (requires ModuleBase):
        __module_config_fields__ = [
            ConfigField("threshold", "float", "Signal Threshold",
                        description="Minimum signal strength", default=0.5,
                        min=0.0, max=1.0),
        ]

        # Option B — enrich existing configurable_fields() return dicts:
        def configurable_fields(self):
            return [{"name": "threshold", "type": "float", "label": "...",
                     "value": self._threshold, "min": 0.0, "max": 1.0}]
    """

    name: str
    type: FieldType
    label: str
    description: str = ""
    default: Any = None
    required: bool = False
    sensitive: bool = False
    # Numeric bounds (float and int types only)
    min: float | None = None
    max: float | None = None
    # Enum options (enum type only)
    options: list[EnumOption] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> ConfigField:
        """Parse from a YAML-deserialized dict (external module config_fields entry)."""
        raw_options = data.get("options", [])
        options = [
            EnumOption.from_dict(o) if isinstance(o, dict) else EnumOption(value=o, label=str(o))
            for o in raw_options
        ]
        return cls(
            name=data["name"],
            type=data.get("type", "str"),
            label=data.get("label", data["name"].replace("_", " ").title()),
            description=data.get("description", ""),
            default=data.get("default"),
            required=data.get("required", False),
            sensitive=data.get("sensitive", False),
            min=data.get("min"),
            max=data.get("max"),
            options=options,
        )

    def to_dict(self, value: Any = _MISSING) -> dict:
        """Serialize to the format expected by the dashboard API.

        Args:
            value: Live current value. When provided (including 0, False, ""),
                   included as 'value' key. Omit entirely for static schema
                   export (no live value available).
        """
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "label": self.label,
            "description": self.description,
            "default": self.default,
            "required": self.required,
            "sensitive": self.sensitive,
        }
        if self.min is not None:
            result["min"] = self.min
        if self.max is not None:
            result["max"] = self.max
        if self.options:
            result["options"] = [o.to_dict() for o in self.options]
        if value is not _MISSING:
            result["value"] = value
        return result


def infer_field_type(value: Any) -> FieldType:
    """Infer a FieldType from a Python value. Used for legacy config conversion."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    return "str"
