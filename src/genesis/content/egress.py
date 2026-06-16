"""Outbound egress gate — deterministic anti-slop scrub + PII scan.

Applied to content destined for an EXTERNAL audience, where the model's own
self-audit can't be trusted and a leak is public-facing. NOT applied to
Genesis talking to the user (Telegram / voice / interactive replies).

Fires when either:
- the delivery channel is external (``email`` / ``discord``), or
- the outreach category is ``content`` — a draft submitted for user review
  before external publishing (so the version the user approves is already
  scrubbed).

Anti-slop (:func:`genesis.content.antislop.scrub`): the spaced em dash is
auto-fixed; the rest is flagged, never deleted. PII (:func:`scan_outbound`):
scanned only for external-DELIVERY channels — a ``content`` review copy headed
to the user is scrubbed for slop but not PII-blocked (it isn't a delivery).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genesis.content.antislop import scrub
from genesis.security.output_scanner import ScanResult, scan_outbound

# Channels whose recipients are an external/third-party audience.
_EXTERNAL_CHANNELS = frozenset({"email", "discord"})
# Outreach category for content drafts under review-before-publish.
_CONTENT_CATEGORY = "content"


@dataclass(frozen=True)
class EgressResult:
    """Outcome of :func:`gate`.

    ``text`` is the (possibly em-dash-scrubbed) content to deliver. ``applied``
    is False when the gate did not fire (text returned unchanged). ``scan`` is
    populated only for external-delivery channels; None otherwise.
    """

    text: str
    applied: bool = False
    fixes_applied: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    scan: ScanResult | None = None

    @property
    def quarantined(self) -> bool:
        """True when a PII scan ran and flagged the content unsafe to send."""
        return self.scan is not None and not self.scan.safe


def should_gate(channel: str, category: str | None) -> bool:
    """Whether outbound content on this channel/category gets the egress gate."""
    return channel in _EXTERNAL_CHANNELS or category == _CONTENT_CATEGORY


def gate(text: str, *, channel: str, category: str | None = None) -> EgressResult:
    """Scrub anti-slop (+ PII-scan external-delivery channels) for egress.

    Returns the content unchanged with ``applied=False`` when the gate does not
    fire. Never raises on content; the caller decides what to do with a
    quarantine (``EgressResult.quarantined``).
    """
    if not should_gate(channel, category):
        return EgressResult(text=text, applied=False)

    scrubbed = scrub(text, is_voiced=True)
    scan = scan_outbound(scrubbed.cleaned_text) if channel in _EXTERNAL_CHANNELS else None
    return EgressResult(
        text=scrubbed.cleaned_text,
        applied=True,
        fixes_applied=scrubbed.fixes_applied,
        flags=scrubbed.flags,
        scan=scan,
    )
