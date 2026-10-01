"""The fixture every scripts/deploy_code_only.sh test runs against.

The REAL script runs as a subprocess against a FIXTURE: a bare upstream and a
clone of it (the ``GENESIS_DEPLOY_ROOT`` seam), a private HOME (so the lock file,
the deploy marker and the alert queue are private), the test interpreter's own
environment as the venv, and PATH shims for ``systemctl`` and ``curl``. No test
touches the real runtime, venv, lock or Guardian.

Import the ``station`` fixture into a test module to use it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy_code_only.sh"
UPDATE_SH = REPO / "scripts" / "update.sh"
# sys.prefix, never a resolved sys.executable: a venv's python is a symlink to the
# base interpreter, and resolving it lands outside the venv.
VENV = Path(sys.prefix)
LOCK_HELD_RC = 200

# The build configuration the dependency gate can certify, and the layout
# install_fixture records by default (its .pth names <source>/src). It goes
# BEFORE [project], so a bare key a test appends still lands in [project].
BUILD_CONFIG = (
    '[build-system]\nrequires = ["setuptools>=64"]\nbuild-backend = "setuptools.build_meta"\n\n'
    '[tool.setuptools.packages.find]\nwhere = ["src"]\n\n'
)
PYPROJECT_OK = BUILD_CONFIG + '[project]\nname = "fixture"\ndependencies = ["packaging"]\n'
PYPROJECT_UNMET = BUILD_CONFIG + '[project]\nname = "fixture"\ndependencies = ["packaging>=9999"]\n'


def exec_file(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@local", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def commit(repo: Path, msg: str, files: dict[str, str] | None = None) -> str:
    for name, body in (files or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(body)
        git(repo, "add", name)
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def systemctl_shim(calls: Path, manifest: Path, on_restart: str = "", booted_at: int = 0) -> str:
    """The station's systemctl: logs argv; `is-active` prints $UNIT_STATE. It models
    a restart the way systemd does: MainPID is the old server's (1111) until a
    restart, then a NEW pid ($NEW_PID, default 2222; "0" = the new process
    exited). A restart also stands in for a completed bootstrap by writing a
    manifest owned by the new pid ($MANIFEST_AFTER, a JSON mapping), unless
    $NO_BOOTSTRAP is set. `on_restart` is extra shell run at the restart.

    `show -p ActiveState` prints $ACTIVE_STATE (default active). `show -p
    ActiveEnterTimestamp` prints the unit's start as systemd does with
    --timestamp=unix: `@$BOOTED_AT` (default *booted_at*, the moment the fixture
    was built) until a restart, then the restart's time ($RESTARTED_AT, or the
    clock)."""
    return (
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        # $UNIT_STATE_LATER: the state every is-active call AFTER the first reports
        # (the first reports $UNIT_STATE) — lets a test hold the unit "active" for
        # the identity check and still end the health wait at once.
        'if [[ " $* " == *" is-active "* ]]; then\n'
        '  s="${UNIT_STATE:-active}"\n'
        f'  if [ -n "${{UNIT_STATE_LATER:-}}" ] && [ -f "{calls}.isactive" ]; then '
        's="$UNIT_STATE_LATER"; fi\n'
        f'  touch "{calls}.isactive"; echo "$s"; [ "$s" = active ]; exit\n'
        "fi\n"
        'if [[ " $* " == *" ActiveState "* ]]; then echo "${ACTIVE_STATE:-active}"; exit 0; fi\n'
        # `show -p WorkingDirectory`: $UNIT_DIR, default the checkout under test
        # ("-" = systemd answers nothing).
        'if [[ " $* " == *" WorkingDirectory "* ]]; then\n'
        '  d="${UNIT_DIR:-${GENESIS_DEPLOY_ROOT:-}}"; [ "$d" = - ] || echo "$d"; exit 0\n'
        "fi\n"
        # `show -p InvocationID`: one id per activation, a new one after a restart
        # ($INVOCATION overrides the first, $INVOCATION_AFTER the one after a
        # restart; "-" = systemd answers nothing).
        'if [[ " $* " == *" InvocationID "* ]]; then\n'
        f'  if [ -f "{calls}.restarted_at" ]; then '
        'echo "${INVOCATION_AFTER:-22222222222222222222222222222222}";\n'
        '  else i="${INVOCATION:-11111111111111111111111111111111}"; [ "$i" = - ] || echo "$i"; fi; exit 0\n'
        "fi\n"
        'if [[ " $* " == *" ActiveEnterTimestamp "* ]]; then\n'
        f'  if [ -f "{calls}.restarted_at" ]; then echo "@$(cat "{calls}.restarted_at")"; '
        f'else echo "@${{BOOTED_AT:-{booted_at}}}"; fi; exit 0\n'
        "fi\n"
        # `show -p ExecStart`: the python the unit runs ($UNIT_PYTHON, default the
        # station's venv; "-" = systemd answers nothing).
        'if [[ " $* " == *" ExecStart "* ]]; then\n'
        f'  p="${{UNIT_PYTHON:-{VENV}/bin/python}}"; [ "$p" = - ] && exit 0\n'
        '  echo "{ path=$p ; argv[]=$p -m genesis serve ${UNIT_ARGS:-} ; ignore_errors=no }"; exit 0\n'
        "fi\n"
        # After a restart, MainPID is $NEW_PID; with $NEW_PID_LATER set, every read
        # after the first one reports that pid instead (the unit restarted again).
        'if [[ " $* " == *" MainPID "* ]]; then\n'
        f'  if grep -q "restart genesis-server" "{calls}"; then\n'
        f'    if [ -n "${{NEW_PID_LATER:-}}" ] && [ -f "{calls}.pidread" ]; then echo "$NEW_PID_LATER";\n'
        f'    else touch "{calls}.pidread"; echo "${{NEW_PID:-2222}}"; fi\n'
        '  else echo "${MAIN_PID:-1111}"; fi; exit 0\n'
        "fi\n"
        # A stop records the HEAD it found, so a test can tell whether it came
        # before or after the fast-forward. $ON_STOP is shell run at the stop:
        # something that changes the tree after the checks and before the merge.
        'if [[ " $* " == *" stop "* ]]; then\n'
        f'  git -C "${{GENESIS_DEPLOY_ROOT:-.}}" rev-parse HEAD > "{calls}.stop_head"\n'
        '  [ -z "${ON_STOP:-}" ] || bash -c "$ON_STOP"\n'
        "fi\n"
        'if [[ " $* " == *" restart "* ]]; then\n'
        f'  echo "${{RESTARTED_AT:-$(date +%s)}}" > "{calls}.restarted_at"\n'
        f"  {on_restart or ':'}\n"
        # $RESTART_RC: the restart stopped the old server, then its start failed.
        '  [ -z "${RESTART_RC:-}" ] || exit "$RESTART_RC"\n'
        '  if [ -z "${NO_BOOTSTRAP:-}" ]; then\n'
        '    m="${MANIFEST_AFTER:-}"\n'
        '    [ -n "$m" ] || m=\'{"db": "ok", "perception": "ok"}\'\n'
        f'    printf \'{{"pid": %s, "manifest": %s}}\' "${{NEW_PID:-2222}}" "$m" > "{manifest}"\n'
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )


def install_fixture(
    site: Path,
    source: Path,
    requires: tuple[str, ...] = ("packaging",),
    *,
    name: str = "fixture",
    editable: bool = True,
    requires_python: str = "",
    entry_points: dict[str, dict[str, str]] | None = None,
    pth: str | None = "src",
) -> None:
    """What `pip install -e <source>` leaves behind, as importlib.metadata reads
    it: a .dist-info with the project's requirements, its entry points
    (entry_points.txt, one section per group), a direct_url.json naming the
    source, and the editable install's .pth listed in RECORD. Put `site` on
    PYTHONPATH and the venv's python finds it first.

    *pth* is the directory under *source* the .pth names, the one line setuptools
    writes for a `packages.find` layout with one `where` (measured, setuptools
    84: `where = ["src"]` writes `<source>/src`). A value starting with "import "
    is written verbatim instead, the shape of a finder-based install; None writes
    no .pth at all."""
    info = site / f"{name}-0.0.0.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    record = [f"{info.name}/METADATA,,", f"{info.name}/direct_url.json,,", f"{info.name}/RECORD,,"]
    if pth is not None:
        pth_name = f"__editable__.{name}-0.0.0.pth"
        body = pth if pth.startswith("import ") else str((source / pth).resolve())
        (site / pth_name).write_text(body + "\n")
        record.append(f"{pth_name},,")
    (info / "RECORD").write_text("\n".join(record) + "\n")
    lines = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 0.0.0"]
    if requires_python:
        lines.append(f"Requires-Python: {requires_python}")
    lines += [f"Requires-Dist: {r}" for r in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n")
    if entry_points:
        (info / "entry_points.txt").write_text(
            "".join(
                f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in eps.items()) + "\n"
                for group, eps in entry_points.items()
            )
        )
    direct = {"url": source.resolve().as_uri()}
    direct["dir_info"] = {"editable": True} if editable else {}
    (info / "direct_url.json").write_text(json.dumps(direct))


def last_reflog_time(repo: Path) -> int:
    line = (repo / ".git" / "logs" / "HEAD").read_text().splitlines()[-1]
    return int(line.split("\t", 1)[0].split()[-2])


@pytest.fixture()
def station(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(upstream)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(seed)], check=True, capture_output=True
    )
    git(seed, "checkout", "-qb", "main")
    commit(seed, "seed", {"pyproject.toml": PYPROJECT_OK, "AGENTS.md": "stats\n"})
    git(seed, "push", "-q", "origin", "main")
    root = tmp_path / "root"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(upstream), str(root)],
        check=True,
        capture_output=True,
    )
    # The server "booted" a second after the clone, so its boot commit is the
    # clone's HEAD (a boot in the SAME second as a move reads as unknown).
    booted_at = last_reflog_time(root) + 1

    shims = tmp_path / "shims"
    shims.mkdir()
    calls = tmp_path / "systemctl.log"
    marker = home / ".genesis" / "update_in_progress.pid"
    manifest = home / ".genesis" / "bootstrap_manifest.json"
    exec_file(shims / "systemctl", systemctl_shim(calls, manifest, booted_at=booted_at))
    # The old server's manifest, as a running install has it before a deploy.
    manifest.write_text('{"pid": 1111, "manifest": {"db": "ok", "perception": "ok"}}')
    # curl answers per $CURL_RC, and records whether the deploy marker was held
    # at the moment the health check ran.
    exec_file(
        shims / "curl",
        "#!/bin/bash\n"
        f'[ -f "{marker}" ] && echo held >> "{tmp_path}/marker_seen"\n'
        f'printf "%s\\n" "$*" >> "{tmp_path}/curl_args"\n'
        "exit ${CURL_RC:-0}\n",
    )
    # The port-ownership probe (GENESIS_DEPLOY_PORT_PROBE, standing in for
    # scripts/lib/port_owned_by.py, which is tested on real sockets in
    # test_port_owned_by.py). Every listener belongs to $PROBE_OWNER (default:
    # the restarted unit, $NEW_PID or 2222); $PROBE_NONE = nothing listening;
    # $PROBE_FOREIGN_TOO = a second listener held by another process. Exit 0
    # means "every listener on the port is <pid>'s". Never the real probe: the
    # live server listens on that port.
    probe = tmp_path / "port_probe.py"
    probe.write_text(
        "import os, sys\n"
        "pid = sys.argv[2]\n"
        "if os.environ.get('PROBE_NONE') or os.environ.get('PROBE_FOREIGN_TOO'):\n"
        "    sys.exit(1)\n"
        "owner = os.environ.get('PROBE_OWNER') or os.environ.get('NEW_PID') or '2222'\n"
        "sys.exit(0 if owner == pid else 1)\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            (
                "GENESIS_",
                "GIT_",
                "UNIT_STATE",
                "ACTIVE_STATE",
                "BOOTED_AT",
                "RESTARTED_AT",
                "CURL_RC",
                "NEW_PID",
                "NO_BOOTSTRAP",
                "SYNC_LOG",
                "MANIFEST_AFTER",
                "PROBE_OWNER",
                "PROBE_NONE",
                "PROBE_FOREIGN_TOO",
                "UNIT_PYTHON",
                "UNIT_ARGS",
                "UNIT_DIR",
                "INVOCATION",
                "INVOCATION_AFTER",
                "RESTART_RC",
                "MAIN_PID",
                "ON_STOP",
                "SYNC_RC",
                "PYTHONPATH",
                "CDPATH",
            )
        )
    }
    site = tmp_path / "site"
    install_fixture(site, root)
    env.update(
        HOME=str(home),
        PATH=f"{shims}:{env['PATH']}",
        PYTHONPATH=str(site),
        GENESIS_DEPLOY_ROOT=str(root),
        GENESIS_DEPLOY_VENV=str(VENV),
        GENESIS_DEPLOY_HEALTH_POLL="1",
        GENESIS_DEPLOY_PORT_PROBE=str(probe),
        GENESIS_ALERT_QUEUE_ROOT=str(home / ".genesis" / "alerts" / "queue"),
    )
    return {
        "env": env,
        "home": home,
        "root": root,
        "seed": seed,
        "tmp": tmp_path,
        "site": site,
        "shims": shims,
        "calls": calls,
        "manifest": manifest,
        "marker": marker,
        "booted_at": booted_at,
        "lock": home / ".genesis" / "locks" / "update.lock",
        "queue": home / ".genesis" / "alerts" / "queue",
    }


_ADVANCES = [0]


def advance_upstream(
    st, msg: str = "upstream advanced", files: dict[str, str] | None = None
) -> str:
    """Push to the fixture's upstream. With no *files* it changes a file the
    server loads (a deploy restarts only for those); pass files to shape it."""
    if files is None:
        _ADVANCES[0] += 1
        files = {"src/genesis/_upstream.py": f"# {msg} {_ADVANCES[0]}\n"}
    tip = commit(st["seed"], msg, files)
    git(st["seed"], "push", "-q", "origin", "main")
    return tip


def run(
    st, *args: str, env: dict | None = None, timeout: float = 60
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--wait", "5", *args],
        env=env or st["env"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def restarted(st) -> bool:
    return st["calls"].exists() and "restart genesis-server" in st["calls"].read_text()


def alerts(st) -> list[Path]:
    return sorted(st["queue"].glob("*.json")) if st["queue"].exists() else []


def assert_untouched(st, head: str, r: subprocess.CompletedProcess) -> None:
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert git(st["root"], "rev-parse", "HEAD") == head, "a refusal moved the tree"
    assert not restarted(st), "a refusal restarted the server"
    assert not st["marker"].exists(), "a refusal left the deploy marker behind"
    assert not alerts(st), "a refusal changed nothing and must not page anyone"
