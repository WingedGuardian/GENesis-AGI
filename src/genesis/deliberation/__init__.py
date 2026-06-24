"""Deliberation — "the chorus" (Track 4, omnipresence layer).

`deliberate()` asks a panel of models and returns a synthesized verdict + the dissent.
"""

from genesis.deliberation.core import deliberate
from genesis.deliberation.types import DeliberationResult, PerModel

__all__ = ["DeliberationResult", "PerModel", "deliberate"]
