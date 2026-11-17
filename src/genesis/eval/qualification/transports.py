"""Two fixed qualification profiles; no response-derived transport selection."""

from . import native, pinned
from .evidence import Incomplete


def select(alias, binding=None, *, config=None):
    """Unbound selection is offline preparation; acquisition needs a marker."""
    if binding is None:
        profile = native if alias == native.ALIAS else pinned
    elif not isinstance(binding, dict):
        raise Incomplete("invalid qualification transport binding")
    elif "transport_kind" not in binding:
        profile = pinned
    elif binding["transport_kind"] == native.KIND and alias == native.ALIAS:
        profile = native
    else:
        raise Incomplete("unknown or inconsistent qualification transport kind")
    if profile is pinned and alias == native.ALIAS:
        raise Incomplete("native qualification requires its explicit transport kind")
    # Retained evidence selection is pure: admission already froze the route.
    # Acquisition/preparation resolve against actual configuration separately.
    if binding is None or config is not None:
        profile.resolve(alias, config)
    return profile
