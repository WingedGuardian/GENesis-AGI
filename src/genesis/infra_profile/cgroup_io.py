"""Cgroup IO settings on user units and scopes: can they take effect here?

``IOWeight`` and its siblings (``IO_KEYS``) are io-controller settings. On a
user unit or a ``systemd-run --user`` scope they take effect only when the user
manager (``user@<uid>.service``) has the io controller. systemd's default
delegation for the user manager commonly leaves io out, and then systemd accepts
the setting and ignores it without a message, so the setting reads as applied
everywhere one looks.

Two readers use this module:

* the storage collector (``collectors/container.py``) records
  ``user_manager_io_delegated`` from :func:`io_delegated`;
* the ``cgroup_io_settings_inert`` posture rule in ``awareness/loop.py`` fires
  on an explicit ``False`` and words its note with :func:`warning_detail`. It
  is informational: alone it is a low-priority posture note, never the high
  posture alert, and it never joins the crash-resilience sentence.

``SETTERS`` names every place Genesis sets one of these keys. A test locks it
against the tracked tree in both directions, so a new site cannot be added
without being named here and a removed one cannot linger.

Advisory only: nothing here blocks or changes a setting. The sites keep their
IO keys because on a host that does delegate io they do take effect.

Stdlib only, and no import of ``genesis.runtime`` (that package's ``__init__``
pulls the whole runtime graph into whichever process collects).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SLUG = "cgroup_io_settings_inert"

#: systemd's io-controller unit settings (systemd.resource-control(5)).
IO_KEYS = (
    "IOWeight",
    "StartupIOWeight",
    "IODeviceWeight",
    "IOReadBandwidthMax",
    "IOWriteBandwidthMax",
    "IOReadIOPSMax",
    "IOWriteIOPSMax",
    "IODeviceLatencyTargetSec",
)


@dataclass(frozen=True)
class Setter:
    """One place Genesis puts an IO key on a user unit or scope."""

    what: str
    path: str
    keys: tuple[str, ...]
    #: "scope" (systemd-run --scope) or "service": labels the site in the note,
    #: which gives both per-process alternatives. No default, so a new entry
    #: must say which it is.
    kind: str


SETTERS: tuple[Setter, ...] = (
    Setter(
        "Claude Code session scopes", "src/genesis/cc/invoker.py", ("IOWeight",), kind="scope"
    ),
    Setter(
        "capped job scopes (genesis.hostmetrics run)",
        "src/genesis/hostmetrics/run.py",
        ("IOWeight",),
        kind="scope",
    ),
    Setter(
        "code-intel index scopes", "scripts/lib/code_intel_scope.sh", ("IOWeight",), kind="scope"
    ),
    Setter(
        "code-intel owner reporter scope",
        "scripts/lib/code_intel_owner.py",
        ("IOWeight",),
        kind="scope",
    ),
)


def io_delegated(sys_root: Path, manager_cgroup: str | None) -> bool | None:
    """Whether the user manager's cgroup has the io controller.

    Reads ``<sys_root>/fs/cgroup/<manager_cgroup>/cgroup.controllers``: the
    controllers available to the manager, which it can then enable for the
    units beneath it. True or False from that file; None when there is no
    manager cgroup or the file cannot be read (cgroup v1, no user manager),
    which must read as unknown, never as "not delegated".
    """
    if not manager_cgroup:
        return None
    path = sys_root / "fs/cgroup" / manager_cgroup.lstrip("/") / "cgroup.controllers"
    try:
        controllers = path.read_text().split()
    except (OSError, UnicodeDecodeError):
        return None
    return "io" in controllers


def warning_detail() -> str:
    """The posture note's text: each site and key, why it is inert, what works."""
    sites = "; ".join(f"{s.what} ({s.kind}, {s.path}: {', '.join(s.keys)})" for s in SETTERS)
    return (
        "the user systemd manager does not have the io cgroup controller (it is "
        "missing from user@<uid>.service's cgroup.controllers), so these IO "
        f"settings on user units and scopes have no effect here: {sites}. systemd "
        "accepts them and ignores them without a message. The per-process "
        "alternative needs no delegation and is inherited by child processes: for "
        "a scope, wrap the command it runs in "
        "`ionice -c2 -n7` (lowest best-effort) or `ionice -c3` (idle), because a "
        "scope takes only resource-control, kill and scope settings and systemd-run "
        "refuses IOSchedulingClass= on it; for a service, set IOSchedulingClass= "
        "and IOSchedulingPriority=. Either needs an I/O scheduler that honours "
        "I/O priority (BFQ; mq-deadline honours the class but not the level). "
        "Delegating io (adding io to Delegate= in a root drop-in for "
        "user@.service) is necessary for the weights but not sufficient: they "
        "also need the BFQ scheduler or the iocost controller (io.cost.qos). "
        "Changing Delegate= usually needs a user-manager restart, which restarts "
        "every user unit, including the Genesis server and every running session. "
        "Informational: nothing is blocked"
    )
