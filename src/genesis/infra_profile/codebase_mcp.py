"""Is codebase-memory-mcp registered but not set up? — ONE answer, two readers.

The repo's ``.mcp.json`` registers ``codebase-memory-mcp`` on every install, and
its launcher (``.claude/mcp/run-codebase-memory``) refuses until an operator has
run the managed ``configure`` and ``enable`` steps by hand
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

The verdict is tri-state on purpose. ``True`` = registered, not opted out, and
not working (the managed settings file is missing, or the query unit is not
persistently enabled). ``False`` = nothing to say (not registered, opted out
with the disable sentinel, or configured and enabled). ``None`` = the question
could not be answered, which must read neither as fine nor as an alert.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

SERVER = "codebase-memory-mcp"
UNIT = "genesis-cbm-query.service"
SLUG = "codebase_mcp_registered_unconfigured"
DOC = "docs/reference/codebase-managed.md"
DOC_SECTION = "If you see the 'registered but unconfigured' alert"
_TIMEOUT = 10.0


class Unanswerable(RuntimeError):
    """A probe that exists could not be completed — not an answer to record."""


def managed_config_path(home: Path) -> Path:
    """The settings path the managed launcher reads (codebase_managed.py main)."""
    return home / ".genesis" / "config" / "codebase-managed.json"


def registered(repo_root: Path) -> bool:
    """Does the checkout's .mcp.json register the server?

    An absent file is an answer (no registration). A file that exists but cannot
    be read or parsed is not: Claude Code cannot load it either, and guessing
    either way would be a fact nobody verified.
    """
    path = repo_root / ".mcp.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError) as exc:
        raise Unanswerable(f"cannot read {path.name}: {exc}") from exc
    try:
        servers = json.loads(text).get("mcpServers")
    except (ValueError, AttributeError) as exc:
        raise Unanswerable(f"{path.name} is not a JSON object: {exc}") from exc
    return isinstance(servers, dict) and SERVER in servers


def disable_file(repo_root: Path) -> Path | None:
    """The kill-switch path, resolved by the ONE shell resolver every other
    consumer uses (scripts/lib/cbm_disable_file.sh), so the override, the ``~/``
    expansion and the fail-closed rules cannot drift here. None = the resolver
    reports no absolute path can be produced (or is missing from the checkout),
    which is a standing property of this environment, not a transient failure.
    """
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


def unit_file_state() -> str | None:
    """UnitFileState of the query unit. None = no systemctl on this box (an
    answer). A unit that does not exist reads "not-found". A systemctl that
    exists and then fails raises Unanswerable."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return None
    try:
        proc = subprocess.run(
            [systemctl, "--user", "show", UNIT, "-p", "LoadState", "-p", "UnitFileState"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unanswerable(f"systemctl show {UNIT} failed: {exc}") from exc
    if proc.returncode != 0:
        raise Unanswerable(f"systemctl show {UNIT} exited {proc.returncode}")
    props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if props.get("LoadState") == "not-found":
        return "not-found"
    return props.get("UnitFileState") or None


def read_facts(
    repo_root: Path,
    home: Path,
    *,
    unit_state: Callable[[], str | None] = unit_file_state,
) -> dict:
    """The facts both readers judge. Raises Unanswerable when one cannot be read."""
    sentinel = disable_file(repo_root)
    config = managed_config_path(home)
    return {
        "registered": registered(repo_root),
        "config_present": config.is_file(),
        "config_path": str(config),
        "unit": UNIT,
        "unit_file_state": unit_state(),
        "disable_file": str(sentinel) if sentinel is not None else None,
        # lexists: anything placed there, even a dangling link, is the owner's
        # opt-out — the same "something is at the path" test the installer uses.
        "disabled": os.path.lexists(sentinel) if sentinel is not None else None,
    }


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
    state = facts.get("unit_file_state")
    if not isinstance(state, str):
        return None  # no unit manager to ask
    return state != "enabled"


def missing_parts(facts: dict) -> list[str]:
    """What exactly is not set up, in words."""
    parts = []
    if facts.get("config_present") is False:
        parts.append(f"the managed settings file {facts.get('config_path')} is missing")
    state = facts.get("unit_file_state")
    if isinstance(state, str) and state != "enabled":
        parts.append(f"{facts.get('unit', UNIT)} is not enabled (state: {state})")
    return parts


def notice(facts: dict) -> str:
    """The one line the installers print. Empty when there is nothing to say."""
    verdict = needs_setup(facts)
    if verdict is True:
        return (
            f"Codebase: NOT SET UP — {SERVER} is registered in .mcp.json but "
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

    def unit_state_or_unknown() -> str | None:
        # Unlike the collector, a printed line hashes nothing, so an unanswerable
        # unit probe is simply "unknown": a missing settings file still decides
        # the verdict, and a present one yields the "could not be checked" line.
        try:
            return unit_file_state()
        except Unanswerable:
            return None

    try:
        facts = read_facts(Path(args.repo), Path.home(), unit_state=unit_state_or_unknown)
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
