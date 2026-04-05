"""Shared systemd helpers for subprocess calls.

When Genesis runs inside a systemd service, DBUS_SESSION_BUS_ADDRESS and
XDG_RUNTIME_DIR may not be inherited. Without them, ``systemctl --user``
cannot communicate with the user's session manager.
"""

from __future__ import annotations

import os


def systemctl_env() -> dict[str, str]:
    """Build environment dict for ``systemctl --user`` subprocess calls.

    Safe to call unconditionally — on systems where the variables are already
    set, this is a no-op copy.
    """
    env = os.environ.copy()
    uid = os.getuid()
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    return env
