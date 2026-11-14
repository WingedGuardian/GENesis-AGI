"""The live-move repair kit and its installer (#3492 D2a-1).

The repair runs from a kit installed OUTSIDE the checkout, because the checkout
is what it may have to repair. scripts/lib/live_repair_kit.py installs it as one
verified unit from HEAD's blobs (never the working tree), keyed by the files'
object ids, under ~/.genesis/lib/live-repair.d/<kit_id>, with `current` a
relative symlink swapped by rename(2). The names come from KIT_SOURCES, one
literal tuple in live_move_repair.py, read with ast (the tree is never run).

Every test installs into a scratch HOME (the installer takes <home> as a
required argument, K13); this directory's conftest asserts the real
~/.genesis/lib is unchanged by the run.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import advance_upstream
from tests.test_scripts._deploy_station import git as station_git
from tests.test_scripts._deploy_station import run as station_run
from tests.test_scripts._live_repair_world import KIT_FILES, _git_env
from tests.test_scripts._update_source import update_text

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib"
INSTALLER = LIB / "live_repair_kit.py"
REPAIR = LIB / "live_move_repair.py"

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="posix-only")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def kitmod():
    return _load(INSTALLER, "_test_live_repair_kit")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


def _commit_sources(repo: Path, overrides: dict[str, str] | None = None, msg: str = "kit") -> str:
    """Commit the ten kit sources (the real files, or *overrides*) under scripts/lib/."""
    lib = repo / "scripts" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    for name in KIT_FILES:
        if overrides and name in overrides:
            (lib / name).write_text(overrides[name])
        else:
            shutil.copy2(LIB / name, lib / name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg, "--allow-empty")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _commit_sources(root)
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "-I", "-S", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        env=env or _git_env(),
        timeout=120,
    )


def _current(home: Path) -> Path:
    return home / ".genesis" / "lib" / "live-repair" / "current"


def _versioned(home: Path) -> Path:
    return home / ".genesis" / "lib" / "live-repair.d"


def _ids(home: Path) -> list[str]:
    d = _versioned(home)
    return sorted(p.name for p in d.iterdir() if not p.name.startswith(".")) if d.exists() else []


def _bump(repo: Path, n: int) -> str:
    """A new commit changing one kit source (alert_queue.sh), so a new kit_id."""
    body = (LIB / "alert_queue.sh").read_text() + f"# bump {n}\n"
    return _commit_sources(repo, {"alert_queue.sh": body}, msg=f"bump {n}")


# ── T-D12: install ───────────────────────────────────────────────────────────


def test_install_takes_heads_blobs_never_the_working_tree(kitmod, repo, home):
    (repo / "scripts" / "lib" / "deploy_marker.sh").write_text("# a dirty local copy\n")
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 0, r.stdout + r.stderr
    cur = _current(home)
    assert cur.is_symlink()
    target = os.readlink(cur)
    assert target.startswith("../live-repair.d/"), target
    kit = cur.resolve()
    manifest = json.loads((kit / "KIT.json").read_text())
    assert manifest["schema"] == 1
    assert manifest["commit"] == _git(repo, "rev-parse", "HEAD")
    assert manifest["kit_id"] == kit.name
    assert sorted(manifest["kit_sources"]) == sorted(KIT_FILES)
    assert len(manifest["kit_sources"]) == 10
    assert set(manifest["files"]) == set(KIT_FILES)
    for name in KIT_FILES:
        data = (kit / name).read_bytes()
        assert data == (LIB / name).read_bytes(), f"{name} is not HEAD's blob"
        assert manifest["files"][name] == _git(repo, "rev-parse", f"HEAD:scripts/lib/{name}")
        assert (kit / name).stat().st_mode & 0o777 == 0o644
    assert kitmod.status(str(repo), str(home))["state"] == "ok"


def test_a_second_install_is_a_no_op(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    first = _ids(home)
    before = {p: p.stat().st_mtime_ns for p in _current(home).resolve().iterdir()}
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 0, r.stderr
    assert _ids(home) == first
    assert {p: p.stat().st_mtime_ns for p in _current(home).resolve().iterdir()} == before


def test_keep_two_versioned_kits(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    first = _ids(home)
    _bump(repo, 1)
    assert _cli("install", str(repo), str(home)).returncode == 0
    second = _ids(home)
    assert len(second) == 2 and set(first) < set(second), "the old kit is kept"
    _bump(repo, 2)
    assert _cli("install", str(repo), str(home)).returncode == 0
    third = _ids(home)
    assert len(third) == 2
    assert first[0] not in third, "the oldest kit is pruned"
    assert _current(home).resolve().name in third


@pytest.mark.parametrize("point", ["tmp-written", "dir-renamed", "link-made"])
def test_a_killed_install_leaves_a_whole_kit_and_the_next_converges(kitmod, repo, home, point):
    assert kitmod.install(str(repo), str(home)) == 0
    old = _current(home).resolve()
    _bump(repo, 1)

    def die(name):
        if name == point:
            raise KeyboardInterrupt(name)

    kitmod._step = die
    with pytest.raises(KeyboardInterrupt):
        kitmod.install(str(repo), str(home))
    now = _current(home).resolve()
    assert now == old, "current moved before the swap"
    assert kitmod.status(str(repo), str(home))["state"] == "stale", "a whole old kit"
    kitmod._step = lambda name: None
    assert kitmod.install(str(repo), str(home)) == 0
    assert kitmod.status(str(repo), str(home))["state"] == "ok"
    d = _versioned(home)
    leftovers = [p.name for p in d.iterdir() if p.name.startswith(".tmp-")]
    leftovers += [
        p.name for p in _current(home).parent.iterdir() if p.name.startswith("current.tmp.")
    ]
    assert leftovers == [], leftovers


def test_a_foreign_current_is_moved_aside_never_deleted(repo, home):
    cur = _current(home)
    cur.mkdir(parents=True)
    (cur / "mine.txt").write_text("someone's\n")
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 0, r.stderr
    assert "WARNING" in r.stdout + r.stderr
    assert cur.is_symlink()
    aside = [p for p in cur.parent.iterdir() if p.name.startswith("current.foreign-")]
    assert len(aside) == 1 and (aside[0] / "mine.txt").read_text() == "someone's\n"


def test_a_corrupted_kit_is_set_aside_and_reinstalled(kitmod, repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    kit = _current(home).resolve()
    (kit / "deploy_marker.sh").write_text("# altered\n")
    assert kitmod.status(str(repo), str(home))["state"] == "corrupt"
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 0, r.stderr
    assert kitmod.status(str(repo), str(home))["state"] == "ok"
    assert (_current(home).resolve() / "deploy_marker.sh").read_bytes() == (
        LIB / "deploy_marker.sh"
    ).read_bytes()
    assert any(p.name.startswith(f".stale-{kit.name}-") for p in _versioned(home).iterdir())


_MONTH_AGO = 30 * 86400


def _age(path: Path, seconds: int) -> None:
    """Backdate a tree, so a rename that keeps st_mtime shows."""
    t = os.stat(path).st_mtime - seconds
    for p in [path, *path.rglob("*")]:
        os.utime(p, (t, t), follow_symlinks=False)


def test_an_old_foreign_current_set_aside_is_kept_a_week_from_the_move(repo, home):
    cur = _current(home)
    cur.mkdir(parents=True)
    (cur / "mine.txt").write_text("someone's\n")
    _age(cur, _MONTH_AGO)
    assert _cli("install", str(repo), str(home)).returncode == 0
    aside = [p for p in cur.parent.iterdir() if p.name.startswith("current.foreign-")]
    assert len(aside) == 1, "removed by the same install that moved it aside"
    assert (aside[0] / "mine.txt").read_text() == "someone's\n"


def test_an_old_corrupted_kit_set_aside_is_kept_a_week_from_the_move(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    kit = _current(home).resolve()
    (kit / "deploy_marker.sh").write_text("# altered\n")
    _age(kit, _MONTH_AGO)
    assert _cli("install", str(repo), str(home)).returncode == 0
    assert any(p.name.startswith(f".stale-{kit.name}-") for p in _versioned(home).iterdir())


def test_the_prune_takes_only_kit_ids(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    other = _versioned(home) / "notes"
    other.mkdir()
    (other / "keep.txt").write_text("mine\n")
    for n in (1, 2, 3):
        _bump(repo, n)
        assert _cli("install", str(repo), str(home)).returncode == 0
    assert (other / "keep.txt").read_text() == "mine\n"
    assert len(_ids(home)) == 3, "two kits plus the directory that is not one"


@pytest.mark.parametrize("which", ["live-repair.d", "live-repair"])
def test_a_symlinked_kit_directory_is_moved_aside_never_followed(repo, home, tmp_path, which):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "a").mkdir(parents=True)
    (elsewhere / "b").mkdir()
    lib = home / ".genesis" / "lib"
    lib.mkdir()
    (lib / which).symlink_to(elsewhere)
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 0 and "symlink" in r.stdout + r.stderr, r.stdout + r.stderr
    assert sorted(p.name for p in elsewhere.iterdir()) == ["a", "b"], "wrote into the target"
    assert not (lib / which).is_symlink() and (lib / which).is_dir()
    aside = [p for p in lib.iterdir() if p.name.startswith(f"{which}.foreign-")]
    assert len(aside) == 1 and aside[0].is_symlink() and aside[0].resolve() == elsewhere
    assert _cli("status", str(repo), str(home)).returncode == 0


def _dead_pid() -> int:
    proc = subprocess.run(["sh", "-c", "echo $$"], capture_output=True, text=True, check=True)
    return int(proc.stdout)


def test_a_killed_installs_leftovers_are_reaped_by_the_next(repo, home):
    """What a SIGKILL leaves (no handler ran): a partial temp kit and a temp
    link under a pid that is gone. One install converges and removes both."""
    assert _cli("install", str(repo), str(home)).returncode == 0
    pid = _dead_pid()
    part = _versioned(home) / f".tmp-0123456789abcdef-{pid}"
    part.mkdir()
    (part / "deploy_marker.sh").write_text("# half\n")
    link = _current(home).parent / f"current.tmp.{pid}"
    link.symlink_to("../live-repair.d/0123456789abcdef")
    _bump(repo, 1)
    assert _cli("install", str(repo), str(home)).returncode == 0
    assert not part.exists() and not os.path.lexists(link)


@pytest.mark.parametrize("point", ["tmp-written", "link-made"])
def test_a_failed_write_removes_its_own_temp(kitmod, repo, home, point):
    assert kitmod.install(str(repo), str(home)) == 0
    _bump(repo, 1)

    def die(name):
        if name == point:
            raise OSError(f"failed at {name}")

    kitmod._step = die
    assert kitmod.install(str(repo), str(home)) == 1
    left = [p.name for p in _versioned(home).iterdir() if p.name.startswith(".tmp-")]
    left += [p.name for p in _current(home).parent.iterdir() if p.name.startswith("current.tmp.")]
    assert left == [], left


def test_status_names_every_state(kitmod, repo, home):
    st = lambda: kitmod.status(str(repo), str(home))  # noqa: E731
    assert st()["state"] == "missing"
    assert _cli("install", str(repo), str(home)).returncode == 0
    assert st()["state"] == "ok"
    kit = _current(home).resolve()
    (kit / "alert_queue.sh").unlink()
    assert st()["state"] == "partial"
    shutil.copy2(LIB / "alert_queue.sh", kit / "alert_queue.sh")
    os.chmod(kit / "alert_queue.sh", 0o644)
    assert st()["state"] == "ok"
    (kit / "KIT.json").write_text("{not json")
    assert st()["state"] == "corrupt"
    shutil.rmtree(_versioned(home))
    assert st()["state"] == "missing", "a dangling current"
    _current(home).unlink()
    assert _cli("install", str(repo), str(home)).returncode == 0
    _bump(repo, 1)
    s = st()
    assert s["state"] == "stale" and s["differs"] == ["alert_queue.sh"], s
    _current(home).unlink()
    _current(home).mkdir()
    assert st()["state"] == "foreign"
    # The CLI prints the same object.
    r = _cli("status", str(repo), str(home))
    assert r.returncode == 0 and json.loads(r.stdout)["state"] == "foreign", r.stdout


def test_a_feature_branch_head_never_becomes_the_kit(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    before = os.readlink(_current(home))
    _git(repo, "checkout", "-qb", "feature")
    _bump(repo, 1)
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 1 and "not main or live" in r.stdout + r.stderr, r.stdout + r.stderr
    assert os.readlink(_current(home)) == before


def test_home_is_required(repo):
    r = _cli("install", str(repo))
    assert r.returncode == 2 and "usage:" in r.stderr, r.stdout + r.stderr


def test_a_source_missing_at_head_keeps_the_installed_kit(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    before = os.readlink(_current(home))
    _git(repo, "rm", "-q", "scripts/lib/live_move_restore_rows.py")
    _git(repo, "commit", "-qm", "drop one")
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 1 and "live_move_restore_rows.py" in r.stdout + r.stderr
    assert os.readlink(_current(home)) == before


def test_an_unreadable_kit_list_keeps_the_installed_kit(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    before = os.readlink(_current(home))
    src = REPAIR.read_text().replace("KIT_SOURCES = (", "KIT_SOURCES = list((", 1)
    (repo / "scripts" / "lib" / "live_move_repair.py").write_text(src)
    _git(repo, "commit", "-qam", "list not a literal tuple")
    r = _cli("install", str(repo), str(home))
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "the kit list could not be read at HEAD" in out, out
    assert os.readlink(_current(home)) == before


# ── T-D12a: the list is read, never run ─────────────────────────────────────


def test_the_kit_list_is_read_by_ast_never_by_running_heads_module(repo, home):
    src = REPAIR.read_text() + "\nraise SystemExit(9)\n"
    (repo / "scripts" / "lib" / "live_move_repair.py").write_text(src)
    _git(repo, "commit", "-qam", "a module that exits when run")
    r = _cli("install", str(repo), str(home))
    assert r.returncode == 0, r.stdout + r.stderr


# ── T-D12b: the prune skips a kit a running repair holds (F9) ───────────────


def test_the_prune_skips_a_held_kit_and_takes_it_once_released(repo, home):
    assert _cli("install", str(repo), str(home)).returncode == 0
    held_dir = _current(home).resolve()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys\n"
            "fd = open(sys.argv[1])\n"
            "fcntl.flock(fd, fcntl.LOCK_SH)\n"
            "print('HELD', flush=True)\n"
            "sys.stdin.read()\n",
            str(held_dir / "KIT.json"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "HELD"
        _bump(repo, 1)
        assert _cli("install", str(repo), str(home)).returncode == 0
        _bump(repo, 2)
        r = _cli("install", str(repo), str(home))
        assert r.returncode == 0, r.stderr
        assert held_dir.exists(), "a held kit was pruned"
        assert "NOTE" in r.stdout and held_dir.name in r.stdout, r.stdout
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)
    _bump(repo, 3)
    assert _cli("install", str(repo), str(home)).returncode == 0
    assert not held_dir.exists(), "released, the next install prunes it"


# ── T-D15: one kit list, equal to the closure the code loads ─────────────────


def _kit_sources() -> tuple[str, ...]:
    tree = ast.parse(REPAIR.read_text())
    found = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "KIT_SOURCES" for t in n.targets)
    ]
    assert len(found) == 1, "KIT_SOURCES must be ONE top-level assignment"
    value = ast.literal_eval(found[0].value)
    assert isinstance(value, tuple) and all(isinstance(v, str) for v in value)
    assert not any(
        isinstance(n, ast.AugAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "KIT_SOURCES"
        for n in ast.walk(tree)
    ), "KIT_SOURCES is one literal tuple: no +="
    return value


def _closure() -> set[str]:
    """Every file the kit loads from its own directory, from the code itself:
    `_load_kit("x")` / `_sibling("x")` literals and `"$1/x"` sources in the bash
    strings, followed from the entry; and `.`/`source` lines in the shell libs."""
    seen: set[str] = set()
    todo = ["live_move_repair.py"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        text = (LIB / name).read_text()
        if name.endswith(".py"):
            found = re.findall(r'_(?:load_kit|sibling)\("([^"]+)"\)', text)
            found += re.findall(r"\$1/([A-Za-z0-9_.-]+)", text)
        else:
            found = [
                os.path.basename(m)
                for m in re.findall(r'^\s*(?:\.|source)\s+"?([^"\s]+)', text, re.M)
            ]
        todo.extend(found)
    return seen


def test_kit_sources_is_the_closure_and_the_test_worlds_list():
    sources = _kit_sources()
    assert len(sources) == len(set(sources)) == 10
    assert set(sources) == _closure()
    assert set(sources) == set(KIT_FILES)


def test_the_installer_reads_the_same_list(kitmod):
    assert kitmod.read_kit_sources(REPAIR.read_bytes()) == _kit_sources()


def _tier2_shell() -> set[str]:
    m = re.search(r"_tier2=\(([^)]*)\)", update_text())
    assert m, "update.sh has no _tier2 array"
    return set(m.group(1).split())


def test_the_two_tier2_lists_agree_and_hold_no_kit_file():
    """update.sh's _tier2 and deploy_health.TIER2_PATHS are one list in two
    languages. The kit files are NOT tier-2 (deploy_code_only.sh installs the
    kit itself, so a kit change needs no update.sh run, #3492 D2a SF6)."""
    from genesis.observability.snapshots.deploy_health import TIER2_PATHS

    shell = _tier2_shell()
    assert shell == set(TIER2_PATHS)
    kit_paths = {f"scripts/lib/{n}" for n in (*_kit_sources(), "live_repair_kit.py")}
    assert not kit_paths & shell


# ── T-D23: a run stays on the kit it started from ───────────────────────────


def test_a_kit_swapped_mid_run_never_mixes_versions(home):
    d = _versioned(home)
    old, new = d / "old", d / "new"
    for kit in (old, new):
        kit.mkdir(parents=True)
        for name in KIT_FILES:
            shutil.copy2(LIB / name, kit / name)
    (new / "update_state_guard.py").write_text("raise RuntimeError('the swapped kit')\n")
    cur = _current(home)
    cur.parent.mkdir(parents=True)
    os.symlink("../live-repair.d/old", cur)
    mod = _load(cur / "live_move_repair.py", "_test_pinned_repair")
    # The swap lands between the module load and the guard load.
    os.replace(_link(cur.parent, "../live-repair.d/new"), cur)
    guard = mod._load_guard()
    assert hasattr(guard, "state_verdict") or hasattr(guard, "verdict")
    assert Path(guard.__file__).resolve().parent == old.resolve()


def _link(parent: Path, target: str) -> Path:
    tmp = parent / "current.tmp.test"
    os.symlink(target, tmp)
    return tmp


# ── T-D33: repair-move runs the kit's versioned path ────────────────────────

_SPY_KIT = """import json, os, sys
with open(os.path.join(os.environ["HOME"], "spy.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv0": sys.argv[0], "args": sys.argv[1:],
                         "no_bytecode": sys.flags.dont_write_bytecode}) + "\\n")
"""


def test_repair_move_execs_the_versioned_path(tmp_path, home):
    co = tmp_path / "checkout"
    scripts = co / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "deploy_candidates", scripts / "deploy_candidates")
    for lib in ("deploy_marker.sh", "live_system_guard.sh"):
        shutil.copy2(LIB / lib, scripts / "lib" / lib)
    kit = _versioned(home) / "abc123"
    kit.mkdir(parents=True)
    (kit / "live_move_repair.py").write_text(_SPY_KIT)
    cur = _current(home)
    cur.parent.mkdir(parents=True)
    os.symlink("../live-repair.d/abc123", cur)
    env = {**_git_env(home)}
    for args in (["repair-move"], ["repair-move", "--explain"]):
        r = subprocess.run(
            ["bash", str(scripts / "deploy_candidates"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert r.returncode == 0, r.stdout + r.stderr
    calls = [json.loads(ln) for ln in (home / "spy.jsonl").read_text().splitlines()]
    assert len(calls) == 2
    for call in calls:
        assert "/live-repair.d/abc123/" in call["argv0"], call
        assert "/live-repair/current/" not in call["argv0"], call
        assert call["no_bytecode"] == 1, "the kit dir must never get a __pycache__"
    assert not (kit / "__pycache__").exists()


def test_repair_move_with_a_dangling_kit_says_it_is_not_installed(tmp_path, home):
    co = tmp_path / "checkout"
    scripts = co / "scripts"
    (scripts / "lib").mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "deploy_candidates", scripts / "deploy_candidates")
    for lib in ("deploy_marker.sh", "live_system_guard.sh"):
        shutil.copy2(LIB / lib, scripts / "lib" / lib)
    cur = _current(home)
    cur.parent.mkdir(parents=True)
    os.symlink("../live-repair.d/gone", cur)
    r = subprocess.run(
        ["bash", str(scripts / "deploy_candidates"), "repair-move"],
        capture_output=True,
        text=True,
        env=_git_env(home),
        timeout=60,
    )
    assert r.returncode == 1 and "kit is not installed" in r.stderr, r.stderr
    assert "live-repair/current" in r.stderr, r.stderr


# ── T-D28: deploy_code_only.sh keeps the kit at HEAD ────────────────────────


def _seed_kit(st, overrides: dict[str, str] | None = None) -> str:
    files = {}
    for name in KIT_FILES:
        files[f"scripts/lib/{name}"] = (
            overrides[name] if overrides and name in overrides else (LIB / name).read_text()
        )
    return advance_upstream(st, "kit sources", files)


def _station_status(kitmod, st) -> dict:
    return kitmod.status(str(st["root"]), str(st["home"]))


def test_deploy_code_only_pull_installs_heads_kit(kitmod, station):
    _seed_kit(station)
    station_git(station["root"], "pull", "-q", "--ff-only")
    assert kitmod.install(str(station["root"]), str(station["home"])) == 0
    old = _current(station["home"]).resolve().name
    tip = _seed_kit(station, {"alert_queue.sh": (LIB / "alert_queue.sh").read_text() + "# v2\n"})
    assert _station_status(kitmod, station)["state"] == "ok", "control: the tree has not moved"
    r = station_run(station, "pull")
    assert r.returncode == 0, r.stdout + r.stderr
    assert station_git(station["root"], "rev-parse", "HEAD") == tip
    s = _station_status(kitmod, station)
    assert s["state"] == "ok" and s["kit_id"] != old, s


def test_deploy_code_only_restart_installs_heads_kit(kitmod, station):
    _seed_kit(station)
    station_git(station["root"], "pull", "-q", "--ff-only")
    assert kitmod.install(str(station["root"]), str(station["home"])) == 0
    _seed_kit(station, {"alert_queue.sh": (LIB / "alert_queue.sh").read_text() + "# v2\n"})
    station_git(station["root"], "pull", "-q", "--ff-only")
    assert _station_status(kitmod, station)["state"] == "stale", "control: a stale kit"
    r = station_run(station, "restart")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _station_status(kitmod, station)["state"] == "ok"


def test_deploy_code_only_survives_a_head_without_the_kit(station):
    r = station_run(station, "pull")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "NOTE" in r.stdout and "repair kit" in r.stdout, r.stdout
    assert not _current(station["home"]).exists()


def test_deploy_code_only_installs_only_past_the_state_refusal():
    """The kit is the version of the code that wrote the update state, so
    deploy_code_only.sh never installs over a state (its refusal dies first)."""
    text = (REPO / "scripts" / "deploy_code_only.sh").read_text()
    refusal = text.index("# END update-state-refusal")
    install = text.index('lib/live_repair_kit.py" install')
    unmoved = text.index("_checkout_unmoved || _restart_or_refuse\n")
    pull_exit = text.index('if [ "$MODE" = pull ]; then')
    assert refusal < unmoved < install < pull_exit


# ── bootstrap installs the kit ──────────────────────────────────────────────


def _bootstrap_block() -> str:
    text = (REPO / "scripts" / "bootstrap.sh").read_text()
    m = re.search(r"# BEGIN live-repair-kit[^\n]*\n(.*?)# END live-repair-kit", text, re.S)
    assert m, "bootstrap.sh has no live-repair-kit block"
    return m.group(1)


def test_bootstrap_installs_the_kit_after_crash_recovery_and_the_units_check():
    text = (REPO / "scripts" / "bootstrap.sh").read_text()
    block = text.index("# BEGIN live-repair-kit")
    assert text.index("# END crash-recovery") < block
    assert text.index('"$_mu_rc" -eq 4') < block, "after the units check, which promises no change"
    assert block < text.index("--- Checking and installing prerequisites ---")


@pytest.mark.parametrize("with_kit", [True, False])
def test_bootstrap_block_installs_or_warns_and_never_fails(tmp_path, repo, home, with_kit):
    if not with_kit:
        _git(repo, "rm", "-q", "-r", "scripts/lib")
        _git(repo, "commit", "-qm", "no kit")
    script = (
        "set -Eeuo pipefail\n"
        f'SCRIPT_DIR="{REPO / "scripts"}"\nGENESIS_ROOT="{repo}"\n'
        + _bootstrap_block()
        + "echo AFTER\n"
    )
    r = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=_git_env(home), timeout=120
    )
    assert r.returncode == 0 and "AFTER" in r.stdout, r.stdout + r.stderr
    assert _current(home).exists() is with_kit
    assert ("WARNING" in r.stdout) is (not with_kit), r.stdout


# ── T-D29: a rollback reinstalls the restored HEAD's kit ────────────────────


def test_the_rollback_install_sits_after_the_code_guard():
    text = update_text()
    assert text.index("# END rollback-code-guard") < text.index("# BEGIN rollback-kit-install")
    block = re.search(
        r"# BEGIN rollback-kit-install[^\n]*\n(.*?)# END rollback-kit-install", text, re.S
    )
    assert block and '"$checkout_ok" = "true"' in block.group(1)
