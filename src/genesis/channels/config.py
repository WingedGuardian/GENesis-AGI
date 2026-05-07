"""Channel configuration — loads channel defaults from channels.yaml."""

from __future__ import annotations

import logging

from genesis.cc.types import CCModel, EffortLevel
from genesis.mcp.health.settings import _load_yaml_merged

logger = logging.getLogger(__name__)


def load_channel_defaults() -> tuple[CCModel, EffortLevel]:
    """Load default model/effort for Telegram from channels.yaml."""
    data = _load_yaml_merged("channels.yaml")
    tg = data.get("telegram", {})
    if not isinstance(tg, dict):
        tg = {}

    raw_model = tg.get("default_model", "")
    raw_effort = tg.get("default_effort", "")

    try:
        model = CCModel(raw_model)
    except ValueError:
        if raw_model:
            logger.warning(
                "Unknown default_model %r in channels config, falling back to sonnet",
                raw_model,
            )
        model = CCModel.SONNET

    try:
        effort = EffortLevel(raw_effort)
    except ValueError:
        if raw_effort:
            logger.warning(
                "Unknown default_effort %r in channels config, falling back to medium",
                raw_effort,
            )
        effort = EffortLevel.MEDIUM

    logger.info("Channel defaults: model=%s, effort=%s", model.value, effort.value)
    return model, effort
