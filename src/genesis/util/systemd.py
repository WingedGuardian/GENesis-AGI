"""Shared systemd helpers for subprocess calls.

When Genesis runs inside a systemd service, DBUS_SESSION_BUS_ADDRESS and
XDG_RUNTIME_DIR may not be inherited. Without them, ``systemctl --user``
cannot communicate with the user's session manager.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

_LITERAL_ARGUMENTS = "--expand-environment=no"
_REFUSED_AN_OPTION = 1  # systemd-run's exit status for an option it does not know


def _systemd_run_refuses_literal_arguments(
    *, executable: str = "systemd-run", env: dict[str, str] | None = None,
) -> bool:
    """Only a definite refusal counts: systemd-run reads its options in order and
    stops at one it does not know before it reaches ``--version``.

    Any other outcome keeps the option. On a systemd that truly lacks it, the launch's
    own scope probe fails and each launcher's existing fallback or refusal takes over.
    """
    try:
        kwargs = {"capture_output": True, "timeout": 10}
        if env is not None:
            kwargs["env"] = env
        probe = subprocess.run(
            [executable, _LITERAL_ARGUMENTS, "--version"], **kwargs
        )
    except FileNotFoundError:
        return True  # nothing will start a scope
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not ask systemd-run about %s (%s); keeping it", _LITERAL_ARGUMENTS, exc)
        return False
    if probe.returncode not in (0, _REFUSED_AN_OPTION):
        logger.warning(
            "systemd-run answered %s about %s; keeping it", probe.returncode, _LITERAL_ARGUMENTS
        )
    return probe.returncode == _REFUSED_AN_OPTION


def literal_scope_args(
    *, executable: str = "systemd-run", env: dict[str, str] | None = None,
) -> list[str]:
    """What makes ``systemd-run --scope`` pass its command's arguments as written.

    Empty only where systemd-run refuses the option (older than 254) or is absent.
    Asked each time: every caller is about to start a process.
    """
    # Preserve the zero-argument default seam used by ordinary launchers.
    refused = (_systemd_run_refuses_literal_arguments()
               if executable == "systemd-run" and env is None
               else _systemd_run_refuses_literal_arguments(executable=executable, env=env))
    return [] if refused else [_LITERAL_ARGUMENTS]


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
