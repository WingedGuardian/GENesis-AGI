"""Stop the MCP servers a shared Codex app-server daemon is holding (#4302).

Codex threads that run on the shared app-server daemon (the path editor
clients such as the VS Code extension use) each start a full set of stdio
MCP servers, and finished subagent threads can keep those sets alive for
hours (upstream openai/codex#38247). A terminal ``codex`` TUI runs embedded
instead: its subagents share one set and exiting it frees everything
(measured on a live install, 2026-11-10). This tool is the Sentinel's
deterministic remedy for that build-up under ``infra:container_memory_high``.

DRY-RUN BY DEFAULT. It signals only with ``--execute``, which only the action
the owner approves is meant to carry, so a session that drops a flag kills
nothing. That boundary is the Sentinel prompt's rule, NOT an enforcement: the
dispatched session runs with full Bash as the same user and could run
``--execute`` (or ``kill``) itself. The tool adds no capability that session
lacks; it makes the approved remedy deterministic and current.

Targets are resolved when the tool RUNS, never frozen into the approval, so a
proposal approved hours later cannot signal a reused pid. A target is a
process that is ALL of:

- a direct child of a ``codex app-server --listen ...`` process (the shared
  daemon; a plugin broker's stdio ``codex app-server`` has no ``--listen``);
- the leader of its own process group (Codex spawns every stdio MCP server
  with ``ProcessMode::NewGroup``, rmcp-client ``stdio_server_launcher.rs``);
- talking over pipes on stdin and stdout (the MCP stdio transport);
- not a Codex binary (``~/.codex/`` helpers such as code-mode);
- not under a ``claude`` process and not in a systemd ``.service`` unit.

Anything the tool cannot read is SKIPPED and counted, never signalled. Each
group gets SIGTERM, then SIGKILL if any member is left after the grace period
(Codex's own unload uses the same order). Live Codex threads on that daemon
lose those servers until they are reopened: Codex does not respawn a killed
stdio server.

Exit codes: 0 everything targeted is gone (or nothing to do), 3 partial: a
signal was refused, a candidate could not be read, or ``/proc`` could not be
listed, 4 the lever is ``off`` (dry-run and ``--execute`` alike, so a session
that only runs the dry-run sees it). Nothing the tool cannot read ever counts
as done.

Lever: ``codex_mcp_stop.mode`` in ``config/sentinel.yaml`` (+ the
``sentinel.local.yaml`` overlay): ``propose_only`` (default) or ``off``. An
unknown value reads as ``off``. ``GENESIS_CODEX_MCP_STOP_DISABLED=1`` is the
env kill switch. A present section with no valid ``mode`` reads as ``off``; an
absent section is the shipped default, ``propose_only``.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from genesis.util import proc_io

logger = logging.getLogger(__name__)

MODE_PROPOSE_ONLY = "propose_only"
MODE_OFF = "off"
_ENV_KILL_SWITCH = "GENESIS_CODEX_MCP_STOP_DISABLED"
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "sentinel.yaml"

#: SIGTERM -> SIGKILL grace. Codex's own unload waits 2 s; 5 s leaves margin
#: for a Python server's shutdown hooks.
TERM_GRACE_S = 5.0

#: `codex app-server <sub>` subcommands that are NOT the server itself.
_NON_SERVER_SUBCOMMANDS = frozenset(
    {
        "daemon",
        "proxy",
        "generate-ts",
        "generate-json-schema",
        "help",
    }
)

EXIT_OK = 0
EXIT_PARTIAL = 3
EXIT_DISABLED = 4


@dataclass
class Group:
    """One MCP-server process group under the app-server."""

    pgid: int
    starttime: int
    cmd: str
    members: list[int] = field(default_factory=list)
    rss_kb: int = 0


@dataclass
class Scan:
    app_servers: list[int] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # unreadable or excluded, with why
    # Candidates that could not be classified: a structured list, never a
    # substring of the free-text reasons (a process could be named "unreadable").
    uncertain: list[str] = field(default_factory=list)

    def unreadable(self, entry: str) -> None:
        self.skipped.append(entry)
        self.uncertain.append(entry)


def load_mode(path: Path | None = None) -> str:
    """The lever, re-read on every call. Unknown or unreadable reads as ``off``."""
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return MODE_OFF
    cfg_path = path or _CONFIG_PATH
    try:
        import yaml

        from genesis._config_overlay import merge_local_overlay

        raw = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
        # strict: a broken owner overlay raises into the handler below (off),
        # instead of falling back to the shipped propose_only.
        raw = merge_local_overlay(raw or {}, cfg_path, strict=True)
    except Exception:
        logger.warning("Failed to load %s; codex_mcp_stop is off", cfg_path, exc_info=True)
        return MODE_OFF
    if not isinstance(raw, dict):
        return MODE_OFF
    if "codex_mcp_stop" not in raw:
        return MODE_PROPOSE_ONLY  # shipped default when the key is absent
    # Present but null (an overlay that left `codex_mcp_stop:` with its mode
    # commented out) is a present section with no valid mode: off.
    section = raw["codex_mcp_stop"]
    mode = section.get("mode") if isinstance(section, dict) else None
    return mode if mode in (MODE_PROPOSE_ONLY, MODE_OFF) else MODE_OFF


def _proc() -> Path:
    return Path(proc_io._PROC)


def _argv(pid: int) -> list[str] | None:
    try:
        raw = (_proc() / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _pgid(pid: int) -> int | None:
    try:
        stat = (_proc() / str(pid) / "stat").read_bytes().decode("utf-8", "replace")
        return int(stat[stat.rindex(")") + 2 :].split()[2])
    except (OSError, ValueError, IndexError):
        return None


def _rss_kb(pid: int) -> int:
    try:
        for line in (_proc() / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _is_pipe(pid: int, fd: int) -> bool | None:
    """True/False for a readable fd link; None when it cannot be read."""
    try:
        return os.readlink(_proc() / str(pid) / "fd" / str(fd)).startswith("pipe:")
    except OSError:
        return None


_SAFE_LABEL = __import__("re").compile(r"[^A-Za-z0-9._+-]")
_INTERPRETER = __import__("re").compile(r"(?:node|nodejs|bun|deno|python(?:\d+(?:\.\d+)?)?)")
_SCRIPT_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".mts", ".py")


def _safe_label(argv: list[str]) -> str:
    """Program basename, plus the script basename after a known interpreter.

    The dry-run output is read by the Sentinel LLM and lands in its transcript,
    so raw argv (attacker-influenced text from third-party MCP configs, and
    often --token/--api-key values) is never printed. No other argument is
    judged by its shape: an option value or a bare positional argument can be
    a credential whatever it looks like, so a script name prints only when
    argv[0] is an interpreter and the argument ends in a script extension.
    """
    if not argv:
        return "?"
    program = os.path.basename(argv[0])
    parts = [program]
    if _INTERPRETER.fullmatch(program):
        script = next(
            (a for a in argv[1:4] if not a.startswith("-") and a.endswith(_SCRIPT_EXTENSIONS)),
            None,
        )
        if script:
            parts.append(os.path.basename(script))
    return " ".join(_SAFE_LABEL.sub("_", x)[:40] for x in parts)


def _same_uid(pid: int) -> bool | None:
    try:
        return (_proc() / str(pid)).stat().st_uid == os.geteuid()
    except OSError:
        return None


def _all_pids() -> list[int]:
    """Every pid in ``/proc``. Raises ``OSError`` when it cannot be listed: an
    empty list would read as "no app-server" in the scan and as "every group
    gone" after a stop."""
    return [int(p.name) for p in _proc().iterdir() if p.name.isdigit()]


def _is_daemon_app_server(argv: list[str]) -> bool:
    if len(argv) < 2 or os.path.basename(argv[0]) != "codex" or argv[1] != "app-server":
        return False
    if len(argv) > 2 and argv[2] in _NON_SERVER_SUBCOMMANDS:
        return False
    # The live daemon runs as `codex app-server --listen unix` (MEASURED on a
    # live install, 0.167.0). Accept `--listen=<v>` too; stdio and off are not
    # a shared daemon.
    rest = argv[2:]
    for i, a in enumerate(rest):
        if a == "--listen":
            value = rest[i + 1] if i + 1 < len(rest) else ""
        elif a.startswith("--listen="):
            value = a.split("=", 1)[1]
        else:
            continue
        return value not in ("", "off") and not value.startswith("stdio")
    return False


def scan() -> Scan:
    """Read ``/proc`` and classify every child of a shared Codex app-server."""
    result = Scan()
    stats = {pid: proc_io.read_stat(pid) for pid in _all_pids()}
    stats = {pid: s for pid, s in stats.items() if s is not None}
    for pid in sorted(stats):
        argv = _argv(pid)
        if argv and _is_daemon_app_server(argv):
            result.app_servers.append(pid)
    homes = {Path.home() / ".codex"}
    if os.environ.get("CODEX_HOME"):
        homes.add(Path(os.environ["CODEX_HOME"]))
    codex_homes = tuple(os.path.realpath(h) + os.sep for h in homes)
    by_pgid: dict[int, list[int]] = {}
    for pid in stats:
        pg = _pgid(pid)
        if pg is not None:
            by_pgid.setdefault(pg, []).append(pid)

    for app in result.app_servers:
        app_pgid = _pgid(app)
        if app_pgid is None:
            # Without it the same-group exclusion below cannot be applied.
            result.unreadable(f"app-server {app} (process group unreadable)")
            continue
        for pid, (_comm, _state, ppid, _threads, starttime) in sorted(stats.items()):
            if ppid != app:
                continue
            argv = _argv(pid) or []
            label = f"{pid} {_safe_label(argv)}"
            pg = _pgid(pid)
            if pg is None:
                result.unreadable(f"{label} (process group unreadable)")
                continue
            if pg != pid:
                result.skipped.append(f"{label} (not its own process group)")
                continue
            if pg <= 1 or pg == app_pgid:
                result.skipped.append(f"{label} (unsafe process group {pg})")
                continue
            owner = _same_uid(pid)
            if owner is None:
                result.unreadable(f"{label} (owner unreadable)")
                continue
            if not owner:
                result.skipped.append(f"{label} (another user's process)")
                continue
            try:
                exe = os.readlink(_proc() / str(pid) / "exe")
            except OSError:
                result.unreadable(f"{label} (executable unreadable)")
                continue
            if os.path.realpath(exe.removesuffix(" (deleted)")).startswith(codex_homes) or exe.startswith(codex_homes):
                result.skipped.append(f"{label} (Codex helper)")
                continue
            stdin_pipe, stdout_pipe = _is_pipe(pid, 0), _is_pipe(pid, 1)
            if stdin_pipe is None or stdout_pipe is None:
                result.unreadable(f"{label} (stdio unreadable)")
                continue
            if not (stdin_pipe and stdout_pipe):
                result.skipped.append(f"{label} (stdio is not pipes: not an MCP server)")
                continue
            if proc_io.claude_ancestor(pid) is not None:
                result.skipped.append(f"{label} (under a Claude Code session)")
                continue
            unit = proc_io.systemd_unit(pid)
            if unit is None:
                result.unreadable(f"{label} (cgroup unreadable)")
                continue
            if unit.endswith(".service"):
                result.skipped.append(f"{label} (systemd unit {unit})")
                continue
            members = sorted(by_pgid.get(pg, [pid]))
            result.groups.append(
                Group(
                    pgid=pg,
                    starttime=starttime,
                    cmd=_safe_label(argv),
                    members=members,
                    rss_kb=sum(_rss_kb(m) for m in members),
                )
            )
    return result


def _group_alive(pgid: int) -> bool:
    """True while any member of the group is still running.

    A zombie (state ``Z``) has already exited and holds no memory; it stays
    listed only until its parent reaps it, so it does not count as alive
    (MEASURED: a killed child whose parent had not reaped it read as "still
    alive after SIGKILL").
    """
    for pid in _all_pids():
        if _pgid(pid) != pgid:
            continue
        st = proc_io.read_stat(pid)
        if st is not None and st[1] != "Z":
            return True
    return False


def stop(
    groups: list[Group],
    *,
    killpg: Callable[[int, int], None] = os.killpg,
    alive: Callable[[int], bool] = _group_alive,
    sleep: Callable[[float], None] = time.sleep,
    grace_s: float = TERM_GRACE_S,
) -> tuple[list[Group], list[str]]:
    """SIGTERM each group, then SIGKILL what survives. Returns (stopped, failures)."""
    failures: list[str] = []
    signalled: list[Group] = []
    gone_already: list[Group] = []
    for g in groups:
        # Identity check right before signalling: the leader must still be the
        # process the scan saw (pid + start time), or the pid was reused.
        if g.pgid <= 1:
            failures.append(f"{g.pgid}: unsafe process group; skipped")
            continue
        now = proc_io.read_starttime(g.pgid)
        if now is None and not alive(g.pgid):
            gone_already.append(g)  # exited on its own since the scan: done
            continue
        if now != g.starttime:
            failures.append(f"{g.pgid}: leader changed since the scan; skipped")
            continue
        try:
            killpg(g.pgid, signal.SIGTERM)
            signalled.append(g)
        except ProcessLookupError:
            signalled.append(g)  # already gone
        except PermissionError as exc:
            failures.append(f"{g.pgid}: SIGTERM refused ({exc})")
    deadline = time.monotonic() + grace_s
    while any(alive(g.pgid) for g in signalled) and time.monotonic() < deadline:
        sleep(0.2)
    stopped: list[Group] = []
    for g in signalled:
        if alive(g.pgid):
            now = proc_io.read_starttime(g.pgid)
            if now is not None and now != g.starttime:
                failures.append(f"{g.pgid}: leader changed during the grace period; not killed")
                continue
            try:
                killpg(g.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                failures.append(f"{g.pgid}: SIGKILL refused ({exc})")
                continue
            sleep(0.2)
        if alive(g.pgid):
            failures.append(f"{g.pgid}: still alive after SIGKILL")
        else:
            stopped.append(g)
    return gone_already + stopped, failures


def _app_age(pid: int) -> str:
    return proc_io.read_proc_start_iso(pid) or "unknown start"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m genesis.sentinel.codex_mcp_stop",
        description="List (default) or stop the MCP servers held by a shared Codex app-server.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually signal the listed groups (default: dry-run).",
    )
    args = parser.parse_args(argv)

    mode = load_mode()
    if mode == MODE_OFF:
        # Dry-run reports it too, so a Sentinel session that only ever runs the
        # dry-run learns not to propose --execute (exit 4 either way).
        print("codex_mcp_stop: disabled (codex_mcp_stop.mode is off); nothing signalled")
        return EXIT_DISABLED

    try:
        found = scan()
    except OSError as exc:
        print(f"codex_mcp_stop: FAILED: /proc could not be listed ({exc}); nothing signalled")
        return EXIT_PARTIAL
    total_mb = sum(g.rss_kb for g in found.groups) / 1024
    if not found.app_servers:
        print("codex_mcp_stop: no shared Codex app-server running; nothing to stop")
        return EXIT_OK
    # The summary comes FIRST: the Sentinel's executor keeps only the head of
    # an action's stdout, so the listing must never push the answer out.
    verb = "stopping" if args.execute else "would stop"
    print(
        f"codex_mcp_stop: {verb} {len(found.groups)} MCP group(s), "
        f"{total_mb:.0f} MB RSS; {len(found.skipped)} skipped"
    )
    if not args.execute:
        for app in found.app_servers:
            print(f"app-server pid {app} (started {_app_age(app)})")
        for g in found.groups:
            print(
                f"  MCP group {g.pgid}: {len(g.members)} process(es), "
                f"{g.rss_kb / 1024:.0f} MB — {g.cmd}"
            )
        for s in found.skipped:
            print(f"  skipped {s}")
        return EXIT_OK

    uncertain = found.uncertain
    if not found.groups and uncertain:
        # Nothing stopped AND some candidates could not be classified: never
        # report this as done (the dispatch would read as resolved).
        print(f"  FAILED: {len(uncertain)} candidate(s) could not be read; nothing stopped")
        return EXIT_PARTIAL
    try:
        stopped, failures = stop(found.groups)
    except OSError as exc:
        # The liveness check could not list /proc: what was signalled is unknown.
        print(f"  FAILED: /proc could not be listed during the stop ({exc}); outcome unknown")
        return EXIT_PARTIAL
    freed_mb = sum(g.rss_kb for g in stopped) / 1024
    print(
        f"codex_mcp_stop: stopped {len(stopped)} of {len(found.groups)} group(s), "
        f"~{freed_mb:.0f} MB freed, {len(failures)} failed. Codex threads on this "
        f"daemon lose these servers until reopened; subagent-heavy Codex work from a "
        f"terminal `codex` does not build them up."
    )
    for f in failures:
        print(f"  FAILED {f}")
    if uncertain:
        # Readable groups stopped, but these were never classified: they may
        # still hold the memory, so the dispatch must not read as resolved.
        print(f"  FAILED: {len(uncertain)} candidate(s) could not be read and were not stopped")
    return EXIT_OK if not (failures or uncertain) else EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
