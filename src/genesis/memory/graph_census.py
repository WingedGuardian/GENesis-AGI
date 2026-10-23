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
CENSUS_SCHEMA = 2
#: The module a memory server must have imported for its traversals to be recorded.
TELEMETRY_MODULE = "src/genesis/memory/graph_telemetry.py"
#: git's default for gc.reflogExpireUnreachable when the setting is unset.
_DEFAULT_UNREACHABLE_EXPIRY_S = 30 * 86400
#: How far ``btime`` may disagree with the boot time CLOCK_BOOTTIME implies.
_BOOT_TOLERANCE_S = 2.0
_PROC = Path("/proc")


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


def scan_mcp_servers(proc_root: Path = _PROC) -> tuple[list[dict], bool]:
    """Every live ``genesis_mcp_server.py`` process, and whether the scan saw
    everything it needed. A process that exits mid-scan is skipped (it is no
    longer running anything); one of ours whose command line, start time or
    environment cannot be read makes the scan incomplete."""
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
        found.append(
            {
                "pid": pid,
                "server": server,
                "start": start,
                "env_switch": env.get(telemetry._TELEMETRY_OFF_ENV),
                "secrets_path": env.get("SECRETS_PATH"),
                "db_path": env.get("GENESIS_DB_PATH"),
                # For the proof rules (graph_census_proof); never written to a row.
                "argv": tuple(argv),
                "env": {k: env[k] for k in proof.PROOF_ENV_KEYS if k in env},
                "exe": _realpath_or_none(base / "exe"),
                "mnt_ns": _readlink_or_none(base / "ns" / "mnt"),
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


def build_census(
    repo: Path | None = None,
    *,
    proc_root: Path = _PROC,
    live_db: Path | None = None,
    live_held_by: int | None = None,
) -> dict:
    """One census row's metrics. Blocking (``/proc`` and ``git``): run it in a
    thread. ``live_held_by`` is the pid whose connection the row will be written
    through: the live database is the file IT holds open, so a path retargeted
    since that connection opened makes the census incomplete."""
    from genesis.env import genesis_db_path, repo_root

    repo = repo or repo_root()
    live = (live_db or genesis_db_path()).resolve()
    own_ns = _readlink_or_none(proc_root / "self" / "ns" / "mnt")
    procs, complete = scan_mcp_servers(proc_root)
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
    rc, dirty = _git(repo, "status", "--porcelain", "--", "src/genesis/memory/")
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

    memory: list[dict] = []
    others: dict[str, int] = {}
    unclassified = 0
    for proc in procs:
        server = proc["server"]
        if server == "":
            unclassified += 1
            continue
        if server != "memory":
            others[server] = others.get(server, 0) + 1
            continue
        held: list[str] | None = None
        unknown: str | None = None
        if history_ok:
            try:
                # Floor the start: a start inside a move's second then lands on
                # that second, which the reader refuses rather than guesses.
                held = reader._since_boot(reflog, int(proc["start"]), head, cutoff)
            except reader._Unknown as exc:
                unknown = str(exc)
        else:
            unknown = "reflog_unreadable"
        telemetry_ok: bool | None = None
        if held:
            results = [has_telemetry(repo, sha, cache) for sha in held]
            telemetry_ok = None if None in results else all(results)
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
        file_vals = proof.secrets_values(proof.secrets_file(env, main_root=repo), proc["start"])
        foreign, db_identity = proof.db_scope(
            proof.db_paths(env, file_vals, main_root=repo),
            live,
            start=proc["start"],
            open_files=proof.open_file_identities(proc_root, proc["pid"]),
        )
        # The DB rules model main's own server code; with that code unproven, a
        # "foreign" reading is not evidence enough to skip the row.
        foreign = foreign and not source_reasons
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
            }
        )
    return {
        "v": CENSUS_SCHEMA,
        "procs": memory,
        "other_servers": others,
        "unclassified": unclassified,
        "head_telemetry": head_telemetry,
        "head_dirty": head_dirty,
        "complete": complete,
        "reasons": reasons,
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
        metrics = await asyncio.to_thread(build_census, live_held_by=os.getpid())
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
