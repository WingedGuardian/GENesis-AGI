"""Per-channel engagement heuristics — V3 static rules, no learning."""

from __future__ import annotations

from genesis.learning.types import EngagementOutcome, EngagementSignal

# Fixed per-channel heuristics (V3 = no learning, static rules)
CHANNEL_HEURISTICS: dict[str, dict] = {
    "whatsapp": {"engaged_threshold_s": 14400, "ignored_threshold_s": 86400},  # 4h, 24h
    "telegram": {"engaged_threshold_s": 14400, "ignored_threshold_s": 86400},
    "web": {"engaged_threshold_s": 300, "ignored_threshold_s": 86400},  # 5min, 24h
    "terminal": {"engaged_threshold_s": 60, "ignored_threshold_s": 3600},  # 1min, 1h
}

_DEFAULT_HEURISTIC = {"engaged_threshold_s": 14400, "ignored_threshold_s": 86400}


def classify_engagement(
    channel: str,
    response_latency_s: float | None,
    has_reaction: bool = False,
    has_reply: bool = False,
    reply_substantive: bool = False,
) -> EngagementSignal:
    """Classify user engagement based on channel-specific heuristics.

    WhatsApp/Telegram: reply <4h = engaged, reaction = engaged, nothing 24h = ignored
    Web: click-through <5min = engaged, no interaction 24h = ignored
    Terminal: substantive reply <1min = engaged, monosyllabic = neutral, nothing 1h = ignored
    """
    h = CHANNEL_HEURISTICS.get(channel, _DEFAULT_HEURISTIC)
    engaged_thresh = h["engaged_threshold_s"]
    ignored_thresh = h["ignored_threshold_s"]

    outcome = EngagementOutcome.NEUTRAL
    evidence_parts: list[str] = []

    # Reaction always counts as engagement
    if has_reaction:
        outcome = EngagementOutcome.ENGAGED
        evidence_parts.append("reaction received")
    elif channel == "terminal" and has_reply:
        # Terminal: substantive reply within threshold = engaged, monosyllabic = neutral
        if reply_substantive and response_latency_s is not None and response_latency_s <= engaged_thresh:
            outcome = EngagementOutcome.ENGAGED
            evidence_parts.append(f"substantive reply in {response_latency_s:.0f}s")
        else:
            outcome = EngagementOutcome.NEUTRAL
            evidence_parts.append("non-substantive or slow terminal reply")
    elif has_reply and response_latency_s is not None and response_latency_s <= engaged_thresh:
        outcome = EngagementOutcome.ENGAGED
        evidence_parts.append(f"reply in {response_latency_s:.0f}s (<={engaged_thresh}s)")
    elif response_latency_s is not None and response_latency_s > ignored_thresh and not has_reply:
        outcome = EngagementOutcome.IGNORED
        evidence_parts.append(f"no response after {response_latency_s:.0f}s (>{ignored_thresh}s)")
    else:
        outcome = EngagementOutcome.NEUTRAL
        if response_latency_s is not None:
            evidence_parts.append(f"latency {response_latency_s:.0f}s, between thresholds")
        else:
            evidence_parts.append("no latency data, no strong signals")

    return EngagementSignal(
        channel=channel,
        outcome=outcome,
        latency_seconds=response_latency_s,
        evidence="; ".join(evidence_parts) if evidence_parts else "no evidence",
    )
