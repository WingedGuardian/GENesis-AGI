"""Is codebase-memory-mcp registered but not set up? — ONE answer, two readers.

``codebase-memory-mcp`` is registered on every install (the checkout's
``.mcp.json`` and the user-scope ``~/.claude.json``), and its launcher
(``.claude/mcp/run-codebase-memory``) refuses until an operator has run the
managed ``configure`` and ``enable`` steps by hand
(docs/reference/codebase-managed.md). Nothing automates that on purpose:
configure needs an already obtained, digest-pinned binary and never downloads.
So an install that never did it shows the server as "failed to connect" in
every session, with nothing saying why.

Two readers ask the same question and must not drift:

* the infrastructure-profile collector (``collectors/codebase_mcp_facts.py``),
  whose facts feed the ``codebase_mcp_registered_unconfigured`` posture rule in
  ``awareness/loop.py``;
* ``scripts/lib/cbm_setup_notice.sh``, which bootstrap.sh, install.sh and
  update.sh call to print one actionable line (``python -m`` this module).

Both read the facts through :func:`read_facts` and judge them with
:func:`needs_setup`. Stdlib only, so the scripts can run it from a checkout
without the runtime's dependency graph.

Nothing the managed tool already decides is re-derived here. Once a settings
file exists, its validity, its configured kill-switch path and the query unit's
state all come from ``scripts/codebase_managed.py status`` — the tool's own
validation, and its own normalized user-manager environment. Only when no
settings file exists is the kill switch resolved by the default resolver
(``scripts/lib/cbm_disable_file.sh``), the one the installers and indexer use.

The verdict is tri-state on purpose. ``True`` = registered, not opted out, and
not working (the managed settings file is missing or invalid, or the query unit
is not persistently enabled). ``False`` = nothing to say (not registered, opted
out with the kill-switch file, or configured and enabled). ``None`` = the
question could not be answered, which must read neither as fine nor as an alert.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SERVER = "codebase-memory-mcp"
UNIT = "genesis-cbm-query.service"
SLUG = "codebase_mcp_registered_unconfigured"
DOC = "docs/reference/codebase-managed.md"
DOC_SECTION = "If you see the 'registered but unconfigured' alert"
_TIMEOUT = 10.0
# `status` bounds each of its own systemctl calls at 30s; this is the outer bound.
_STATUS_TIMEOUT = 90.0
_MAX_JSON_BYTES = 16 * 1024 * 1024


class Unanswerable(RuntimeError):
    """A probe that exists could not be completed — not an answer to record."""


def managed_config_path(home: Path) -> Path:
    """The settings path the managed launcher reads (codebase_managed.py main)."""
    return home / ".genesis" / "config" / "codebase-managed.json"


def _servers(path: Path) -> dict:
    """The mcpServers mapping of one registration file; {} when it is absent.

    Opened non-blocking and checked with fstat before any read, so a FIFO (or
    any non-regular file) at the path is refused instead of hanging the refresh
    — the same "not a regular file" rule setup_claude_config.py applies to
    .mcp.json. Unreadable or unparseable means "could not check", never "not
    registered".
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise Unanswerable(f"cannot open {path.name}: {exc}") from exc
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unanswerable(f"{path.name} is not a regular file")
        payload = stream.read(_MAX_JSON_BYTES + 1)
    if len(payload) > _MAX_JSON_BYTES:
        raise Unanswerable(f"{path.name} is too large to check")
    try:
        servers = json.loads(payload).get("mcpServers")
    except (ValueError, AttributeError, RecursionError) as exc:
        raise Unanswerable(f"{path.name} is not a JSON object: {exc}") from exc
    return servers if isinstance(servers, dict) else {}


def registrations(repo_root: Path, home: Path) -> dict[str, bool]:
    """Where the server is registered: the checkout (.mcp.json) and user scope
    (~/.claude.json, written by scripts/lib/mcp_register.sh)."""
    return {
        "project": SERVER in _servers(repo_root / ".mcp.json"),
        "user": SERVER in _servers(home / ".claude.json"),
    }


def disable_file(repo_root: Path) -> Path | None:
    """The DEFAULT kill-switch path, resolved by the ONE shell resolver the
    installers and indexer use (scripts/lib/cbm_disable_file.sh). None = the
    resolver reports no absolute path can be produced (or is missing), which is
    a standing property of this environment, not a transient failure."""
    lib = repo_root / "scripts" / "lib" / "cbm_disable_file.sh"
    bash = shutil.which("bash")
    if bash is None or not lib.is_file():
        return None
    try:
        proc = subprocess.run(
            [bash, "-c", '. "$1" && genesis_cbm_disable_file', "_", str(lib)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unanswerable(f"kill-switch resolver failed: {exc}") from exc
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value.startswith("/"):
        return None
    return Path(value)


def managed_status(repo_root: Path, config: Path) -> dict:
    """``codebase_managed.py status`` for the launcher's settings path, run
    isolated with this interpreter (the checkout's venv: the bash helper passes
    it, and the server runs under it). Its failure is "could not check"."""
    script = repo_root / "scripts" / "codebase_managed.py"
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(script), "--config", str(config), "status"],
            capture_output=True,
            text=True,
            timeout=_STATUS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unanswerable(f"managed status failed: {exc}") from exc
    if proc.returncode != 0:
        raise Unanswerable(f"managed status exited {proc.returncode}")
    try:
        value = json.loads(proc.stdout)
    except ValueError as exc:
        raise Unanswerable(f"managed status printed no JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Unanswerable("managed status printed no JSON object")
    return value


def _opt_out(facts: dict, sentinel: Path | None) -> None:
    facts["disable_file"] = str(sentinel) if sentinel is not None else None
    # lexists: anything placed there, even a dangling link, is the owner's
    # opt-out — the launcher's sentinel_armed test is lstat-based too.
    facts["disabled"] = os.path.lexists(sentinel) if sentinel is not None else None


def read_facts(repo_root: Path, home: Path, *, strict_manager: bool = False) -> dict:
    """The facts both readers judge. Raises Unanswerable when one cannot be read.

    Ordered so each answer is reached with the fewest probes: unregistered stops
    first; a missing settings file needs only the default kill switch; an armed
    kill switch stops before any unit state is consulted. ``strict_manager``:
    raise when status could not reach the user manager (the collector, which
    must not hash a guess) instead of recording the unit state as unknown.
    """
    where = registrations(repo_root, home)
    config = managed_config_path(home)
    facts: dict = {
        "registered": where["project"] or where["user"],
        "registered_project": where["project"],
        "registered_user": where["user"],
        "config_path": str(config),
        "unit": UNIT,
    }
    if not facts["registered"]:
        return facts
    facts["config_present"] = os.path.lexists(config)
    if not facts["config_present"]:
        _opt_out(facts, disable_file(repo_root))
        return facts

    status = managed_status(repo_root, config)
    settings = status.get("settings") if isinstance(status.get("settings"), dict) else {}
    configured = settings.get("sentinel")
    if isinstance(configured, str) and configured.startswith("/"):
        _opt_out(facts, Path(configured))  # what the launcher enforces
    else:
        _opt_out(facts, disable_file(repo_root))
    if facts["disabled"] is True:
        return facts
    error = status.get("settings_error")
    facts["settings_error"] = error if isinstance(error, str) else None
    facts["config_valid"] = facts["settings_error"] is None
    if not facts["config_valid"]:
        return facts

    service = status.get("service")
    if not isinstance(service, dict):
        if strict_manager:
            raise Unanswerable(f"managed status: {status.get('manager_error')}")
        facts["unit_file_state"] = None
        return facts
    if service.get("LoadState") == "not-found":
        facts["unit_file_state"] = "not-found"
    else:
        facts["unit_file_state"] = service.get("UnitFileState") or None
    return facts


def needs_setup(facts: dict) -> bool | None:
    """True = alert; False = nothing to say; None = could not tell."""
    if facts.get("registered") is not True:
        return False
    disabled = facts.get("disabled")
    if disabled is True:
        return False
    if disabled is not False:
        return None  # cannot tell whether the owner opted out
    config = facts.get("config_present")
    if config is False:
        return True
    if config is not True:
        return None
    valid = facts.get("config_valid")
    if valid is False:
        return True
    if valid is not True:
        return None
    state = facts.get("unit_file_state")
    if not isinstance(state, str):
        return None  # no unit manager to ask
    return state != "enabled"


def missing_parts(facts: dict) -> list[str]:
    """What exactly is not set up, in words."""
    parts = []
    if facts.get("config_present") is False:
        parts.append(f"the managed settings file {facts.get('config_path')} is missing")
    elif facts.get("config_valid") is False:
        parts.append(
            f"the managed settings file {facts.get('config_path')} is invalid "
            f"({facts.get('settings_error')})"
        )
    state = facts.get("unit_file_state")
    if isinstance(state, str) and state != "enabled":
        parts.append(f"{facts.get('unit', UNIT)} is not enabled (state: {state})")
    return parts


def notice(facts: dict) -> str:
    """The one line the installers print. Empty when there is nothing to say."""
    verdict = needs_setup(facts)
    if verdict is True:
        return (
            f"Codebase: NOT SET UP — {SERVER} is registered but "
            f"{' and '.join(missing_parts(facts))}, so every session shows it as "
            f'failed to connect. Fix: {DOC}, section "{DOC_SECTION}" '
            f"(configure, then enable). Not wanted? Create {facts.get('disable_file')}"
        )
    if verdict is None and facts.get("registered") is True:
        return (
            "Codebase: setup state could not be checked; run "
            ".venv/bin/python -I scripts/codebase_managed.py status"
        )
    if facts.get("registered") is True and facts.get("disabled") is False:
        return "Codebase: OK"
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m genesis.infra_profile.codebase_mcp")
    parser.add_argument("--repo", required=True, help="the checkout whose .mcp.json to read")
    args = parser.parse_args(argv)
    try:
        # Not strict: a printed line hashes nothing, so an unreachable manager is
        # simply "unknown" — a missing or invalid settings file still decides it.
        facts = read_facts(Path(args.repo), Path.home())
    except Unanswerable as exc:
        print(
            f"Codebase: setup state could not be checked ({exc}); run "
            ".venv/bin/python -I scripts/codebase_managed.py status"
        )
        return 0
    line = notice(facts)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
