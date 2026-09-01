"""Federation — the private, human-in-the-loop Genesis↔Genesis relay (v1).

A turn-gated, cross-owner, cross-install channel between two DIFFERENT users'
Genesis installs. Requires no SSH and no access to sensitive data on the peer.
Secure-by-design, least-privilege, HITL: the agent PROPOSES, only the human
COMMITS; the user's approval always trumps a peer's urgency.

Standalone subsystem (NOT a channels/outreach adapter — the counterparty is an
UNTRUSTED peer, inverting every other channel's owner-trusted model). Reuses
Genesis primitives: the approval-hold gate (owner approval), the content
sanitizer (inbound injection perimeter), and provenance quarantine (peer content
is external-untrusted). Transport is swappable; v1 ships a MockTransport for
tests and a PyNaCl-relay client behind the same interface.
"""
