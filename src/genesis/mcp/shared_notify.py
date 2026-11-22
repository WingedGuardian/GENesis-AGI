"""Notify the local service manager only after shared startup has completed."""

import os
import socket


def notify_ready():
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address[0] not in {"/", "@"}:
        raise ValueError("Shared MCP readiness requires a local notification socket")
    if address.startswith("@"):
        address = "\0" + address[1:]
    payload = b"READY=1"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
        if sender.sendto(payload, address) != len(payload):
            raise OSError("Incomplete shared MCP readiness notification")
