"""Tests for guardian approval server singleton fix."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from genesis.guardian.approval import ApprovalServer, _ApprovalState


class TestApprovalState:
    """Unit tests for _ApprovalState."""

    def test_create_and_approve(self):
        state = _ApprovalState()
        token = state.create_token(expiry_s=60)
        assert token is not None
        assert state.approved is False
        assert state.try_approve(token) is True
        assert state.approved is True

    def test_wrong_token_rejected(self):
        state = _ApprovalState()
        state.create_token(expiry_s=60)
        assert state.try_approve("wrong-token") is False
        assert state.approved is False

    def test_double_approve_rejected(self):
        state = _ApprovalState()
        token = state.create_token(expiry_s=60)
        assert state.try_approve(token) is True
        assert state.try_approve(token) is False  # Already used

    def test_expired_token_rejected(self):
        state = _ApprovalState()
        token = state.create_token(expiry_s=0)  # Immediate expiry
        time.sleep(0.01)
        assert state.try_approve(token) is False

    def test_create_token_resets_state(self):
        state = _ApprovalState()
        token1 = state.create_token(expiry_s=60)
        state.try_approve(token1)
        assert state.approved is True

        token2 = state.create_token(expiry_s=60)
        assert state.approved is False
        assert token2 != token1


class TestApprovalServerIsolation:
    """Tests that each ApprovalServer has its own state (singleton fix)."""

    def _make_config(self, port: int) -> MagicMock:
        cfg = MagicMock()
        cfg.bind_host = "127.0.0.1"
        cfg.port = port
        cfg.token_expiry_s = 3600
        return cfg

    def test_two_servers_independent_state(self):
        """Two ApprovalServer instances must not share state."""
        server_a = ApprovalServer(self._make_config(18881))
        server_b = ApprovalServer(self._make_config(18882))

        # Each has its own _state
        assert server_a._state is not server_b._state

    def test_token_from_one_server_invalid_on_another(self):
        """Token created by server A must not validate on server B."""
        server_a = ApprovalServer(self._make_config(18883))
        server_b = ApprovalServer(self._make_config(18884))

        token_a = server_a._state.create_token(expiry_s=60)
        # Try to approve token_a on server_b's state
        assert server_b._state.try_approve(token_a) is False
        # But it works on server_a's state
        assert server_a._state.try_approve(token_a) is True

    def test_second_server_does_not_invalidate_first(self):
        """Starting a second server must not overwrite first server's token."""
        server_a = ApprovalServer(self._make_config(18885))
        token_a = server_a._state.create_token(expiry_s=60)

        # Creating a token on server_b should not affect server_a
        server_b = ApprovalServer(self._make_config(18886))
        server_b._state.create_token(expiry_s=60)

        # server_a's token should still be valid
        assert server_a._state.try_approve(token_a) is True

    def test_is_approved_uses_instance_state(self):
        """is_approved property must check instance state, not module state."""
        server = ApprovalServer(self._make_config(18887))
        assert server.is_approved is False

        token = server._state.create_token(expiry_s=60)
        server._state.try_approve(token)
        assert server.is_approved is True


class TestApprovalServerHTTP:
    """Integration test for the HTTP approval flow."""

    def _make_config(self, port: int) -> MagicMock:
        cfg = MagicMock()
        cfg.bind_host = "127.0.0.1"
        cfg.port = port
        cfg.token_expiry_s = 3600
        return cfg

    def test_http_approval_flow(self):
        """Full HTTP flow: start server, approve via HTTP, verify."""
        import urllib.request

        server = ApprovalServer(self._make_config(18888))
        try:
            url = server.start()
            assert "/approve/" in url

            # Hit the approval URL
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200

            assert server.is_approved is True
        finally:
            server.stop()

    def test_http_wrong_token_rejected(self):
        """Wrong token returns 404."""
        import urllib.request

        server = ApprovalServer(self._make_config(18889))
        try:
            server.start()

            bad_url = "http://127.0.0.1:18889/approve/wrong-token"
            req = urllib.request.Request(bad_url)
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 404
            assert server.is_approved is False
        finally:
            server.stop()

    def test_two_http_servers_isolated(self):
        """Two HTTP servers on different ports don't share state."""
        import json
        import urllib.request

        server_a = ApprovalServer(self._make_config(18890))
        server_b = ApprovalServer(self._make_config(18891))
        try:
            url_a = server_a.start()
            server_b.start()  # Start but don't need its URL

            # Approve server A
            with urllib.request.urlopen(url_a, timeout=5) as resp:
                data = json.loads(resp.read())
                assert data["status"] == "approved"

            # Server A approved, server B still not
            assert server_a.is_approved is True
            assert server_b.is_approved is False
        finally:
            server_a.stop()
            server_b.stop()
