"""Sentinel — Genesis's container-side guardian.

The Guardian watches from outside (host VM). The Sentinel watches from inside
(container). In the ideal state, the Sentinel handles everything and the Guardian
stays in HEALTHY — the Guardian becomes the "container is truly dead" backup.

The Sentinel is an autonomous CC call site — it diagnoses infrastructure problems
and FIXES them. After mechanical reflexes (remediation registry) prove
insufficient, the Sentinel dispatches a CC session that investigates, requests
approval via Telegram, and executes the fix.
"""

from genesis.sentinel.classifier import FireAlarm, classify_alerts, worst_tier
from genesis.sentinel.dispatcher import SentinelDispatcher, SentinelRequest, SentinelResult
from genesis.sentinel.state import SentinelState, SentinelStateData

__all__ = [
    "FireAlarm",
    "SentinelDispatcher",
    "SentinelRequest",
    "SentinelResult",
    "SentinelState",
    "SentinelStateData",
    "classify_alerts",
    "worst_tier",
]
