"""Hourly census of memory-server processes: when the FalkorDB cutover clock can start.

The cutover verdict needs every traversal of its 14-day window on record, but a
memory server started before the telemetry code was deployed traverses without
writing anything, and nothing else records when such a process dies. So once an
hour genesis-server lists the live ``genesis_mcp_server.py --server memory``
processes and, for each, every commit the main checkout has held since that
process started (from the checkout's reflog), and whether ALL of them contain
``graph_telemetry.py``. Every one counts because a module imported after start
loads whatever is on disk then, so a process can run a mix.

The verdict starts the clock at the first census with no memory server that
could be running older code, and restarts it when the census goes missing for
more than one slot. This module records facts only; the verdict decides what is
clean. Row schema ``v`` = ``CENSUS_SCHEMA``.

The commit history comes from ``scripts/lib/serving_commit.py``, the reader the
deploy scripts already trust for "which commit did a process start from": it
refuses (and the census records why) on a reflog gap, a move in the process's
start second, a clock that stepped back, or a start older than git's expiry
cutoff for unreachable entries.

Schema 2 ("conservative unknowns"): a memory server's row is clean only when
its code SOURCE, its kill SWITCH and its database SCOPE are proven, as well as
its history above. ``graph_census_proof`` holds those rules and says what each
one can and cannot prove: the source from the venv its ``argv[0]`` names and
what that venv and ``PYTHONPATH`` can load; the switch it actually runs with
(the startup router reloads its secrets file with override=True, so a proven
file carrying the key decides; launch and running disagreeing, or an unprovable
file, is unknown); the scope by every database's file identity. Anything
unproven lands in ``unknown_reasons`` (or ``telemetry_off: None``) and the
verdict reads the row as not clean, so the cutover clock waits for a restart.
``/proc/<pid>/environ`` is the environment at exec, which is why a later
``os.environ`` load is re-derived from the file rather than read. genesis-server
reads ``secrets.env`` only when its unit starts, so each census also reads the
file itself and says ``telemetry_disabled`` once the switch is set there.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from genesis.memory import graph_census_proof as proof
from genesis.memory import graph_telemetry as telemetry

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

logger = logging.getLogger(__name__)

#: ``eval_events.event_type`` of a census row; pruned with the telemetry rows.
CENSUS_EVENT_TYPE = "graph_traverse_census"
#: Bumped whenever a field's meaning changes, so a reader can tell rows apart.
CENSUS_SCHEMA = 3
#: The module a memory server must have imported for its traversals to be recorded.
TELEMETRY_MODULE = "src/genesis/memory/graph_telemetry.py"
#: git's default for gc.reflogExpireUnreachable when the setting is unset.
_DEFAULT_UNREACHABLE_EXPIRY_S = 30 * 86400
#: How far ``btime`` may disagree with the boot time CLOCK_BOOTTIME implies.
_BOOT_TOLERANCE_S = 2.0
_PROC = Path("/proc")
#: How far back the census-level history check looks: one :45 slot plus the
#: verdict's gap tolerance and margin, so a process that lived and died between
#: two censuses still had every commit it could have loaded checked.
HISTORY_LOOKBACK_S = 2.5 * 3600
#: The scripts whose processes traverse the graph by inserting their own tree's
#: src (role -> script under the main checkout).
_SELF_SRC_SCRIPTS = {"ambient": "ambient_awareness_worker.py"}
_REPLAY_SCRIPT = "ambient_replay.py"
#: What a traversal producer loads code from in the checkout: an edit here
#: means a process may run code no commit records.
_RUNTIME_PATHS = (
    "src/",
    "scripts/genesis_mcp_server.py",
    "scripts/ambient_awareness_worker.py",
)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _boot_epoch(proc_root: Path) -> float | None:
    """``btime`` from ``/proc/stat``: wall-clock seconds at boot. Not
    ``/proc/uptime``, which a container may virtualise (measured 28 s off on an
    LXC host)."""
    try:
        for line in (proc_root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _boot_disagrees(boot: float) -> bool:
    """True when ``btime`` is off from the boot time CLOCK_BOOTTIME implies. A
    late ``btime`` would make every process look younger than it is, which is
    the one error that could start the cutover clock early."""
    derived = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
    return abs(boot - derived) > _BOOT_TOLERANCE_S


def _start_epoch(pid: int, boot: float, proc_root: Path) -> float | None:
    from genesis.runtime.init.process_reaper import proc_starttime_ticks

    ticks = proc_starttime_ticks(pid, proc_root)
    if ticks is None:
        return None
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def _read_nul_list(path: Path) -> list[str]:
    return [part.decode("utf-8", "replace") for part in path.read_bytes().split(b"\0") if part]


def _realpath_or_none(path: Path) -> str | None:
    try:
        return os.path.realpath(os.readlink(path))
    except OSError:
        return None


def _readlink_or_none(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _interpreter_dir(argv0: str, env: dict[str, str]) -> str | None:
    """The real directory of the interpreter ``argv0`` named, resolved the way
    exec did: as given when absolute, else through that process's own PATH (an
    activated venv puts its bin first). ``""`` when exec resolved it against the
    cwd AT EXEC, which the process may have left: a relative path with a slash,
    or a relative or empty PATH entry reached before an absolute match."""
    if os.path.isabs(argv0):
        return os.path.realpath(os.path.dirname(argv0))
    if "/" in argv0:
        return ""
    for d in env.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            return ""  # exec searched the cwd at exec here first
        if os.access(os.path.join(d, argv0), os.X_OK):
            return os.path.realpath(d)
    return None


def _other_role(
    argv: list[str],
    venv_bin: str | None,
    env: dict[str, str] | None = None,
) -> str | None:
    """The role of a python process that is not an MCP server, or None when it
    is none the census counts: ambient worker, replay, another bridge or server,
    or any other process on main's venv interpreter (``venv-generic``). Only an
    interpreter counts: an editor or ``tail`` naming the worker's file does not."""
    if not argv or not os.path.basename(argv[0]).startswith("python"):
        return None
    script = proof._script(tuple(argv))
    if script is not None and os.path.basename(script) == _REPLAY_SCRIPT:
        return "replay"
    role = telemetry.process_role(argv)
    # process_role matches the worker's name anywhere in argv (it labels rows);
    # here the entry script decides, so `pytest .../ambient_awareness_worker.py`
    # is not a worker.
    if role == "ambient" and (
        script is None or os.path.basename(script) != _SELF_SRC_SCRIPTS["ambient"]
    ):
        role = None
    if role in ("ambient", "server", "bridge"):
        return role
    if venv_bin is None:
        return None
    resolved = _interpreter_dir(argv[0], env or {})
    # An unresolvable interpreter might be main's: counted, as unknown.
    return "venv-generic" if resolved in (venv_bin, "") else None


def scan_mcp_servers(
    proc_root: Path = _PROC, *, main_root: Path | None = None, skip_pid: int | None = None
) -> tuple[list[dict], bool]:
    """Every live ``genesis_mcp_server.py`` process, and (given ``main_root``)
    every other traversal producer ``_other_role`` names, and whether the scan
    saw everything it needed. A process that exits mid-scan is skipped (it is no
    longer running anything); one of ours whose command line, start time or
    environment cannot be read makes the scan incomplete. ``skip_pid`` is the
    census's own host, which gets its own row."""
    venv_bin = os.path.realpath(main_root / ".venv" / "bin") if main_root else None
    boot = _boot_epoch(proc_root)
    if boot is None:
        return [], False
    found: list[dict] = []
    complete = True
    try:
        pids = sorted(int(p.name) for p in proc_root.iterdir() if p.name.isdigit())
    except OSError:
        return [], False
    for pid in pids:
        if pid == skip_pid:
            continue
        base = proc_root / str(pid)
        try:
            argv = _read_nul_list(base / "cmdline")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            try:
                if base.stat().st_uid == os.getuid():
                    complete = False  # one of ours, unreadable: could be a server
            except OSError:
                pass
            continue
        server = telemetry.mcp_server_name(argv)
        if server is None:
            if not (argv and os.path.basename(argv[0]).startswith("python")):
                continue
            # An absolute interpreter is classified from argv alone: a process on
            # another interpreter (another user's service, say) is not read further.
            if os.path.isabs(argv[0]) and _other_role(argv, venv_bin) is None:
                continue
            try:
                if base.stat().st_uid != os.getuid():
                    continue  # another user's process cannot run as this install
            except OSError:
                continue
        start = _start_epoch(pid, boot, proc_root)
        try:
            env_pairs = _read_nul_list(base / "environ")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            env_pairs = None
        if start is None or env_pairs is None:
            if (base / "cmdline").exists():
                complete = False
            continue
        env = dict(pair.split("=", 1) for pair in env_pairs if "=" in pair)
        role = None
        if server is None:
            role = _other_role(argv, venv_bin, env)
            if role is None:
                continue
        found.append(
            {
                "pid": pid,
                "server": server,
                "role": role,
                "start": start,
                "env_switch": env.get(telemetry._TELEMETRY_OFF_ENV),
                "secrets_path": env.get("SECRETS_PATH"),
                "db_path": env.get("GENESIS_DB_PATH"),
                # For the proof rules (graph_census_proof); never written to a row.
                "argv": tuple(argv),
                "env": {
                    k: env[k] for k in env if k in proof.PROOF_ENV_KEYS or k.startswith("PYTHON")
                },
                "exe": _realpath_or_none(base / "exe"),
                "mnt_ns": _readlink_or_none(base / "ns" / "mnt"),
                # Resolved here, with the full env (the kept subset has no PATH).
                "interpreter_unresolved": bool(argv)
                and _interpreter_dir(argv[0], env) == "",
            }
        )
    return found, complete


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """A read-only git call that never takes ``index.lock``: an hourly ``git
    status`` must not collide with a deploy's pull on the live checkout."""
    from genesis.observability.git_health import _CHEAP_TIMEOUT_S, _run_git

    rc, out, _ = _run_git(repo, "--no-optional-locks", *args, timeout=_CHEAP_TIMEOUT_S)
    return rc, out


def has_telemetry(repo: Path, sha: str, cache: dict[str, bool | None]) -> bool | None:
    """Whether ``sha`` contains the telemetry module; ``None`` if ``sha`` is not
    a commit this repository can read."""
    if sha not in cache:
        if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] != 0:
            cache[sha] = None
        else:
            cache[sha] = _git(repo, "cat-file", "-e", f"{sha}:{TELEMETRY_MODULE}")[0] == 0
    return cache[sha]


def _load_serving_commit() -> ModuleType | None:
    """``scripts/lib/serving_commit.py`` from the tree this code runs from (the
    deploy scripts run the same file; stdlib only). ``None`` if it cannot be
    loaded, which leaves every memory server without a verdict."""
    from genesis.env import repo_root

    path = repo_root() / "scripts" / "lib" / "serving_commit.py"
    try:
        spec = importlib.util.spec_from_file_location("genesis._serving_commit", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        logger.warning("serving_commit reader not loadable at %s", path, exc_info=True)
        return None


def _expiry_cutoff(repo: Path) -> int | None:
    """Unix seconds before which git may have expired unreachable reflog entries
    (as ``scripts/lib/deploy_status.sh`` computes it); ``None`` if unreadable."""
    rc, out = _git(repo, "config", "--type=expiry-date", "gc.reflogExpireUnreachable")
    if rc == 0:
        try:
            return int(out.strip())
        except ValueError:
            return None
    if rc == 1:  # unset: git's 30-day default
        return int(time.time()) - _DEFAULT_UNREACHABLE_EXPIRY_S
    return None


def _read_reflog(repo: Path) -> str | None:
    rc, git_dir = _git(repo, "rev-parse", "--absolute-git-dir")
    if rc != 0:
        return None
    try:
        return (Path(git_dir.strip()) / "logs" / "HEAD").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None


def host_source_reasons(main_root: Path) -> list[str]:
    """``[]`` when every genesis module THIS process has loaded came from
    ``main_root``'s src: observed directly, so no interpreter rule is needed
    for the census's own host."""
    import sys

    src = os.path.realpath(main_root / "src") + os.sep
    for name, module in list(sys.modules.items()):
        if name != "genesis" and not name.startswith("genesis."):
            continue
        origin = getattr(module, "__file__", None)
        if origin and not os.path.realpath(origin).startswith(src):
            return ["host_source_not_main"]
    return []


def build_census(
    repo: Path | None = None,
    *,
    proc_root: Path = _PROC,
    live_db: Path | None = None,
    host_pid: int | None = None,
    include_host: bool = False,
    live_held_by: int | None = None,
    host_home: Path | None = None,
    prior: dict[tuple[int, str], str] | None = None,
) -> dict:
    """One census row's metrics. Blocking (``/proc`` and ``git``): run it in a
    thread. ``host_pid`` is this process, skipped by the scan; with
    ``include_host`` it gets a row of its own (``record_census`` asks).
    ``live_held_by`` is the pid whose connection the row will be written
    through: the live database is the file IT holds open, so a path retargeted
    since that connection opened makes the census incomplete."""
    from genesis.env import genesis_db_path, genesis_home, repo_root

    repo = repo or repo_root()
    live = (live_db or genesis_db_path()).resolve()
    host_home = host_home or genesis_home()
    own_ns = _readlink_or_none(proc_root / "self" / "ns" / "mnt")
    procs, complete = scan_mcp_servers(proc_root, main_root=repo, skip_pid=host_pid)
    reasons: list[str] = [] if complete else ["proc_unreadable"]
    if live_held_by is not None:
        held = proof.open_file_identities(proc_root, live_held_by)
        try:
            ident = (live.stat().st_dev, live.stat().st_ino)
        except OSError:
            ident = None
        if held is None or ident not in held:
            complete = False
            reasons.append("live_db_moved")

    if proc_root == _PROC:
        boot = _boot_epoch(proc_root)
        if boot is None or _boot_disagrees(boot):
            complete = False
            reasons.append("start_time_math")

    cache: dict[str, bool | None] = {}
    rc, head = _git(repo, "rev-parse", "HEAD")
    head = head.strip() if rc == 0 else ""
    head_telemetry = has_telemetry(repo, head, cache) if head else None
    # The history rule reasons about COMMITS, and the proofs model the loaders
    # (the entry scripts, routing, env): an edit anywhere a producer loads code
    # from voids both, not only one inside the memory package.
    rc, dirty = _git(repo, "status", "--porcelain", "--", *_RUNTIME_PATHS)
    head_dirty = bool(dirty.strip()) if rc == 0 else None
    if head_telemetry is None or head_dirty is None:
        complete = False
        reasons.append("head_unreadable")

    reader = _load_serving_commit()
    reflog = _read_reflog(repo)
    cutoff = _expiry_cutoff(repo)
    history_ok = reader is not None and reflog is not None and cutoff is not None and bool(head)
    if not history_ok:
        complete = False
        reasons.append("reflog_unreadable")

    def history(start: float) -> tuple[list[str] | None, bool | None, str | None]:
        """(commits HEAD held since ``start``, all with telemetry?, why unknown)."""
        if not history_ok:
            return None, None, "reflog_unreadable"
        try:
            # Floor the start: a start inside a move's second then lands on
            # that second, which the reader refuses rather than guesses.
            held = reader._since_boot(reflog, int(start), head, cutoff)
        except reader._Unknown as exc:
            return None, None, str(exc)
        if not held:
            return held, None, None
        results = [has_telemetry(repo, sha, cache) for sha in held]
        return held, (None if None in results else all(results)), None

    memory: list[dict] = []
    others: dict[str, int] = {}
    unclassified = 0
    for proc in procs:
        server = proc["server"]
        if server == "":
            unclassified += 1
            continue
        if server is None:
            row = _producer_row(
                proc, repo=repo, live=live, own_ns=own_ns, history=history,
                proc_root=proc_root, host_home=host_home,
            )
            if row is not None:
                memory.append(row)
            continue
        if server != "memory":
            others[server] = others.get(server, 0) + 1
            continue
        held, telemetry_ok, unknown = history(proc["start"])
        env = proc["env"]
        view = proof.ProcView(
            pid=proc["pid"],
            argv=proc["argv"],
            env=env,
            start=proc["start"],
            exe=proc["exe"],
            mnt_ns=proc["mnt_ns"],
        )
        source_reasons = proof.interpreter_source(
            view,
            main_root=repo,
            own_mnt_ns=own_ns,
            script=repo / "scripts" / "genesis_mcp_server.py",
        )
        # Each value derived the way THIS server derives it (its own SECRETS_PATH,
        # GENESIS_REPO_ROOT and HOME), never from the census process's state.
        secrets_path = proof.secrets_file(env, main_root=repo)
        file_vals = proof.secrets_values(secrets_path, proc["start"])
        carried = False
        if file_vals is None and prior:
            # Changed since start: still proven if an earlier row for this same
            # server proved the same census-relevant values (`carry_secrets`).
            file_vals = proof.carry_secrets(
                secrets_path,
                proc["start"],
                prior.get((proc["pid"], _iso(proc["start"]))),
                env=env,
                main_root=repo,
            )
            carried = file_vals is not None
        foreign, db_identity = proof.db_scope(
            proof.db_paths(env, file_vals, main_root=repo),
            live,
            start=proc["start"],
            open_files=proof.open_file_identities(proc_root, proc["pid"]),
        )
        # The DB rules model main's own server code; with that code unproven, a
        # "foreign" reading is not evidence enough to skip the row.
        foreign = foreign and not source_reasons
        # Its graph engine socket and lost-writes file come from ITS home.
        if not foreign and not proof.same_home(proof.genesis_home_of(env, file_vals), host_home):
            source_reasons = [*source_reasons, "other_genesis_home"]
        memory.append(
            {
                "pid": proc["pid"],
                "role": "mcp-memory",
                "started_at": _iso(proc["start"]),
                "commit": held[0] if held else None,
                "held": held,
                "telemetry": telemetry_ok,
                "unknown": unknown,
                "source": proof.SOURCE_MAIN if not source_reasons else None,
                "unknown_reasons": source_reasons,
                "telemetry_off": proof.switch_off(env, file_vals),
                "foreign": foreign,
                "db_identity": db_identity,
                # The proof the next census may carry forward; a digest, never a value.
                "secrets_digest": proof.secrets_digest(file_vals) if file_vals is not None else None,
                "carried": carried,
            }
        )
    if include_host and host_pid is not None:
        memory.append(_host_row(host_pid, repo=repo, proc_root=proc_root, history=history))

    # Every commit HEAD held in the lookback, whatever ran then: a worker or a
    # server restart that lived and died between censuses left no row.
    head_history: bool | None = None
    if history_ok:
        try:
            window = _window_held(
                reader, reflog, time.time() - HISTORY_LOOKBACK_S, head, cutoff
            )
        except reader._Unknown:
            reasons.append("history_unreadable")
        else:
            results = [has_telemetry(repo, sha, cache) for sha in window]
            head_history = None if None in results else all(results)
    return {
        "v": CENSUS_SCHEMA,
        "procs": memory,
        "other_servers": others,
        "unclassified": unclassified,
        "head_telemetry": head_telemetry,
        "head_dirty": head_dirty,
        "head_history_telemetry": head_history is True,
        "complete": complete,
        "reasons": reasons,
    }


def _window_held(reader, reflog: str, since: float, head: str, cutoff: int) -> list[str]:
    """Every commit HEAD held after ``since``. Read from the moves themselves,
    not via a "boot" lookup, so a reflog that is empty or starts inside the
    window (no move to anchor on) still answers: no move since ``since`` means
    HEAD held only HEAD. Raises ``reader._Unknown`` on a gap, a clock that went
    backwards, or a reflog that does not end at HEAD, and when the window
    starts before ``cutoff`` (git may have expired a detour there in a pair,
    leaving no gap)."""
    if since < cutoff:
        raise reader._Unknown("the window starts before git's reflog expiry cutoff")
    moves = reader._moves(reflog)
    if moves and moves[-1][1] != head:
        raise reader._Unknown("the reflog does not end at HEAD")
    # Reflog times are whole seconds, so a move in the second the window starts
    # in may be after `since`: count it rather than miss a detour there.
    edge = int(since)
    first = next((i for i, m in enumerate(moves) if m[2] >= edge), None)
    if first is None:
        return [head]
    if any(when < edge for _, _, when in moves[first:]):
        raise reader._Unknown("reflog times go backwards inside the window")
    for i in range(first + 1, len(moves)):
        if moves[i][0] != moves[i - 1][1]:
            raise reader._Unknown("the reflog has a gap inside the window")
    held = [moves[first][0]] + [new for _, new, _ in moves[first:]]
    return [sha for sha in held if sha.strip("0")]  # 000… is "no commit" (a clone)


def _producer_row(
    proc: dict, *, repo: Path, live: Path, own_ns, history, proc_root: Path,
    host_home: Path,
) -> dict | None:
    """The row of a non-MCP traversal producer, by role."""
    role = proc["role"]
    base = {"pid": proc["pid"], "role": role, "started_at": _iso(proc["start"])}
    if role == "replay":
        # Owner ruling ba877469: replay re-runs historical turns, not live traffic.
        return {**base, "excluded": "replay_not_live_traffic"}
    if role in ("server", "bridge"):
        # The census runs inside the host stack; another one beside it is a
        # fault (two pollers), and its loader is not ours to prove.
        return {**base, "unknown_reasons": ["runs_alongside_host"], "telemetry": None}
    held, telemetry_ok, unknown = history(proc["start"])
    env = proc["env"]
    # Generic and ambient processes load no secrets file: the launch env is what
    # they run with (READ: session_awareness/worker.py; the hook loaded dotenv
    # into the env it spawned them with).
    foreign, db_identity = proof.db_scope(
        [proof._resolve_db(env, main_root=repo)],
        live,
        start=proc["start"],
        open_files=proof.open_file_identities(proc_root, proc["pid"]),
    )
    if own_ns is None or proc["mnt_ns"] != own_ns:
        foreign = False  # its paths may name other files in its namespace
    if proc.get("interpreter_unresolved"):
        # exec resolved it against a cwd that may be gone: maybe main's venv.
        telemetry_ok, unknown, foreign = None, "interpreter_unresolved", False
    elif proof.options_unmodelled(proc["argv"]):
        # An option that shifts what reads as the script: it may be a server.
        telemetry_ok, unknown, foreign = None, "interpreter_options_unmodelled", False
    elif (
        not foreign
        and own_ns is not None
        and proc["mnt_ns"] == own_ns  # another namespace is the documented residual
        and not proof.same_home(proof.genesis_home_of(env), host_home)
    ):
        # Its graph engine socket and lost-writes file come from ITS home.
        telemetry_ok, unknown = None, "other_genesis_home"
    row = {
        **base,
        "commit": held[0] if held else None,
        "held": held,
        "telemetry": telemetry_ok,
        "unknown": unknown,
        "foreign": foreign,
        "db_identity": db_identity,
    }
    if role == "venv-generic":
        return row  # owner ruling ba877469: history and scope only
    view = proof.ProcView(
        pid=proc["pid"],
        argv=proc["argv"],
        env=env,
        start=proc["start"],
        exe=proc["exe"],
        mnt_ns=proc["mnt_ns"],
    )
    reasons = proof.interpreter_source(
        view,
        main_root=repo,
        own_mnt_ns=own_ns,
        script=repo / "scripts" / _SELF_SRC_SCRIPTS[role],
    )
    row.update(
        source=proof.SOURCE_MAIN if not reasons else None,
        unknown_reasons=reasons,
        telemetry_off=env.get(telemetry._TELEMETRY_OFF_ENV) == "1",
        foreign=foreign and not reasons,
    )
    return row


def _host_row(pid: int, *, repo: Path, proc_root: Path, history) -> dict:
    """This process's own row, proven from where its modules actually loaded.
    Its switch is read in-process by ``record_census`` (a disabled host writes
    the ``telemetry_disabled`` row instead), and its database is the live one."""
    boot = _boot_epoch(proc_root)
    start = _start_epoch(pid, boot, proc_root) if boot is not None else None
    held, telemetry_ok, unknown = history(start) if start is not None else (None, None, "no_start")
    reasons = host_source_reasons(repo)
    return {
        "pid": pid,
        "role": telemetry.process_role(),
        "host": True,
        "started_at": _iso(start) if start is not None else None,
        "commit": held[0] if held else None,
        "held": held,
        "telemetry": telemetry_ok,
        "unknown": unknown,
        "source": proof.SOURCE_MAIN if not reasons else None,
        "unknown_reasons": reasons,
        "telemetry_off": False,
        "foreign": False,
        "db_identity": None,
    }


def _disabled_in_secrets() -> bool:
    """Whether ``secrets.env`` sets the kill switch now (read on every census,
    not inherited from the server's start). Unreadable counts as not set: the
    file is optional, and the per-process environ read still applies."""
    from genesis.env import secrets_path

    try:
        from dotenv import dotenv_values

        return dotenv_values(secrets_path()).get(telemetry._TELEMETRY_OFF_ENV) == "1"
    except Exception:
        logger.debug("secrets.env unreadable for the census kill-switch check", exc_info=True)
        return False


def prior_digests(metrics: dict | None) -> dict[tuple[int, str], str]:
    """``{(pid, started_at): secrets_digest}`` from ONE earlier census row's
    metrics, for `build_census`'s carry-forward. Only a row of the current schema
    counts; anything malformed counts for nothing (no carry), never for a guess."""
    if not isinstance(metrics, dict) or metrics.get("v") != CENSUS_SCHEMA:
        return {}
    procs = metrics.get("procs")
    if not isinstance(procs, list):
        return {}
    out: dict[tuple[int, str], str] = {}
    for row in procs:
        if not isinstance(row, dict):
            continue
        pid, started, digest = row.get("pid"), row.get("started_at"), row.get("secrets_digest")
        if type(pid) is int and isinstance(started, str) and isinstance(digest, str) and digest:
            out[(pid, started)] = digest
    return out


async def _latest_prior(db: aiosqlite.Connection) -> dict[tuple[int, str], str]:
    """The carry-forward map from the single latest census row, or empty. Only
    the latest: a row that cannot be used (unreadable, disabled, another schema)
    means no carry, never a reach past it to an older one."""
    from genesis.db.crud import j9_eval

    try:
        rows = await j9_eval.get_events(db, event_type=CENSUS_EVENT_TYPE, limit=1)
    except Exception:
        logger.warning("graph census: the previous row could not be read; no carry", exc_info=True)
        return {}
    return prior_digests(rows[0].get("metrics") if rows else None)


async def record_census(db: aiosqlite.Connection) -> dict:
    """Write one census row. With the telemetry kill switch on in THIS process,
    the row says so instead of going silent, so the verdict can tell "disabled"
    from "genesis-server was down"."""
    from genesis.db.crud import j9_eval

    if telemetry._telemetry_off() or await asyncio.to_thread(_disabled_in_secrets):
        metrics: dict = {
            "v": CENSUS_SCHEMA,
            "procs": [],
            "complete": False,
            "reasons": ["telemetry_disabled"],
        }
    else:
        prior = await _latest_prior(db)
        metrics = await asyncio.to_thread(
            lambda: build_census(
                host_pid=os.getpid(), include_host=True, live_held_by=os.getpid(), prior=prior
            )
        )
    await j9_eval.insert_event(
        db, dimension="system", event_type=CENSUS_EVENT_TYPE, metrics=metrics
    )
    return metrics


def _wire_graph_census(scheduler, rt) -> None:
    """Register the hourly census on the learning scheduler. ``:45`` is free of
    the other hourly jobs; CronTrigger because IntervalTrigger restarts its
    count on every server restart."""
    from apscheduler.triggers.cron import CronTrigger

    from genesis.env import user_timezone

    async def _graph_traverse_census() -> None:
        if rt._db is None:
            return
        try:
            await record_census(rt._db)
            rt.record_job_success("graph_traverse_census")
        except Exception as exc:
            rt.record_job_failure("graph_traverse_census", exc=exc)
            logger.exception("graph_traverse census failed")

    scheduler.add_job(
        _graph_traverse_census,
        CronTrigger(minute=45, timezone=user_timezone()),
        id="graph_traverse_census",
        max_instances=1,
        misfire_grace_time=600,
    )
