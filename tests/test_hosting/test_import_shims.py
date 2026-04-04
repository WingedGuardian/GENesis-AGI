"""Verify backward-compat shims — all old import paths still resolve."""

from __future__ import annotations


def test_az_client_shim():
    from genesis.channels.az_client import AZClient
    from genesis.hosting.agent_zero.client import AZClient as AZClientCanonical

    assert AZClient is AZClientCanonical


def test_notification_bridge_shim():
    from genesis.hosting.agent_zero.notification_bridge import (
        NotificationBridge as NBCanonical,
    )
    from genesis.observability.az_bridge import NotificationBridge

    assert NotificationBridge is NBCanonical


def test_notification_bridge_observability_init_shim():
    from genesis.hosting.agent_zero.notification_bridge import (
        NotificationBridge as NBCanonical,
    )
    from genesis.observability import NotificationBridge

    assert NotificationBridge is NBCanonical


def test_memory_compat_shim():
    from genesis.hosting.agent_zero.memory_compat import (
        doc_to_payload as dtp_canonical,
    )
    from genesis.hosting.agent_zero.memory_compat import (
        payload_to_doc as ptd_canonical,
    )
    from genesis.memory.az_adapter import doc_to_payload, payload_to_doc

    assert doc_to_payload is dtp_canonical
    assert payload_to_doc is ptd_canonical


def test_memory_compat_private_shim():
    """Private _generate_id must still be importable from old path (test_az_adapter uses it)."""
    from genesis.hosting.agent_zero.memory_compat import _generate_id as canonical
    from genesis.memory.az_adapter import _generate_id

    assert _generate_id is canonical


def test_ui_blueprint_shim():
    from genesis.hosting.agent_zero.overlay import (
        blueprint as canonical_bp,
    )
    from genesis.hosting.agent_zero.overlay import (
        register_injection as canonical_ri,
    )
    from genesis.ui.blueprint import blueprint, register_injection

    assert blueprint is canonical_bp
    assert register_injection is canonical_ri
