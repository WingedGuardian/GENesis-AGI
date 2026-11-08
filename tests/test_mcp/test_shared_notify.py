"""Actual owned datagrams and failure behavior of the stdlib readiness helper."""

import importlib
import select
import socket
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("value", [None, ""])
def test_manual_start_does_not_open_a_notification_socket(monkeypatch, value):
    module = importlib.import_module("genesis.mcp.shared_notify")
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    if value is not None:
        monkeypatch.setenv("NOTIFY_SOCKET", value)
    opened = Mock(side_effect=AssertionError("manual mode opened a socket"))
    monkeypatch.setattr(module.socket, "socket", opened)
    module.notify_ready()
    opened.assert_not_called()


@pytest.mark.parametrize("kind", ["path", "abstract"])
def test_notify_sends_exact_packet_to_owned_receiver(monkeypatch, tmp_path, kind):
    module = importlib.import_module("genesis.mcp.shared_notify")
    value = str(tmp_path / "n") if kind == "path" else "@genesis-" + uuid.uuid4().hex
    address = value if kind == "path" else "\0" + value[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(address)
        monkeypatch.setenv("NOTIFY_SOCKET", value)
        module.notify_ready()
        assert select.select([receiver], [], [], 0)[0]
        assert receiver.recv(64) == b"READY=1"
        assert not select.select([receiver], [], [], 0)[0]


@pytest.mark.parametrize("value", ["relative.sock", "http://localhost"])
def test_malformed_notification_address_refuses(monkeypatch, value):
    module = importlib.import_module("genesis.mcp.shared_notify")
    monkeypatch.setenv("NOTIFY_SOCKET", value)
    with pytest.raises(ValueError):
        module.notify_ready()


def test_missing_notification_receiver_is_an_error(monkeypatch, tmp_path):
    module = importlib.import_module("genesis.mcp.shared_notify")
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "absent"))
    with pytest.raises(FileNotFoundError):
        module.notify_ready()


@pytest.mark.parametrize("failure", ["send", "short"])
def test_notification_send_failure_closes_socket_and_propagates(monkeypatch, failure):
    module = importlib.import_module("genesis.mcp.shared_notify")
    monkeypatch.setenv("NOTIFY_SOCKET", "/unused-owned-fixture")
    closed = []

    @contextmanager
    def opened(*args):
        send = Mock(side_effect=OSError("synthetic send failure")) if failure == "send" else Mock(return_value=0)
        try:
            yield SimpleNamespace(sendto=send)
        finally:
            closed.append(True)

    monkeypatch.setattr(module.socket, "socket", opened)
    with pytest.raises(OSError):
        module.notify_ready()
    assert closed == [True]


def test_socket_creation_failure_propagates(monkeypatch):
    module = importlib.import_module("genesis.mcp.shared_notify")
    monkeypatch.setenv("NOTIFY_SOCKET", "/unused-owned-fixture")
    monkeypatch.setattr(module.socket, "socket", Mock(side_effect=OSError("synthetic open failure")))
    with pytest.raises(OSError, match="synthetic open failure"):
        module.notify_ready()
