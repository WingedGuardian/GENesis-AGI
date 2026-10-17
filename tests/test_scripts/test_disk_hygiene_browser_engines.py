"""scripts/disk_hygiene.sh ``prune_browser_engines``: it deletes only what
scripts/prune_browser_engines.py prints, and only an engine path inside the
camoufox cache root, in the order printed.

The decision itself is tested in test_prune_browser_engines.py; here a stub
interpreter prints the paths, so the shell's own checks are what is exercised.
"""

from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _stub(tmp_path: Path, paths: list[Path]) -> Path:
    listing = tmp_path / "printed"
    listing.write_bytes(b"".join(str(p).encode() + b"\0" for p in paths))
    stub = tmp_path / "python"
    stub.write_text(f'#!/bin/sh\ncat "{listing}"\n')
    stub.chmod(0o755)
    return stub


def _prune(root: Path, stub: Path, *, prefix: str = "") -> subprocess.CompletedProcess:
    """GENESIS_HOME under tmp: the function takes the provisioner's lock there."""
    home = root.parent / "gh"
    script = (
        f"source '{_HYGIENE}'\nVENV_PY='{stub}'\nexport GENESIS_HOME='{home}'\n"
        f"{prefix}prune_browser_engines '{root}'"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _tree(root: Path) -> dict[str, Path]:
    made = {
        "version": root / "browsers" / "official" / "156.0.1-beta.30-aa",
        "legacy_file": root / "libxul.so",
        "legacy_dir": root / "browser",
        "deep": root / "browsers" / "official" / "156.0.1-beta.30-aa" / "sub",
        "repo": root / "browsers" / "official",
    }
    for key in ("version", "legacy_dir", "deep"):
        made[key].mkdir(parents=True, exist_ok=True)
    made["legacy_file"].write_bytes(b"x")
    return made


def test_engine_paths_inside_the_root_are_removed(tmp_path):
    root = tmp_path / "camoufox"
    made = _tree(root)
    r = _prune(root, _stub(tmp_path, [made["legacy_file"], made["legacy_dir"], made["version"]]))
    assert r.returncode == 0, r.stderr
    assert not made["legacy_file"].exists() and not made["legacy_dir"].exists()
    assert not made["version"].exists() and made["repo"].is_dir()


def test_anything_else_printed_is_refused(tmp_path):
    root = tmp_path / "camoufox"
    made = _tree(root)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = _prune(
        root, _stub(tmp_path, [outside, made["deep"], made["repo"], root, root / "browsers"])
    )
    assert r.returncode == 0, r.stderr
    assert outside.is_dir() and made["deep"].is_dir() and made["repo"].is_dir() and root.is_dir()
    assert (root / "browsers").is_dir()
    assert r.stdout.count("refused") == 5


def test_no_interpreter_skips(tmp_path):
    root = tmp_path / "camoufox"
    _tree(root)
    r = _prune(root, tmp_path / "missing-python")
    assert r.returncode == 0 and "SKIPPED" in r.stdout


def test_an_invisible_process_table_skips(tmp_path):
    """Hidden processes would read as "no browser runs from it"."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    empty_proc = tmp_path / "proc"
    empty_proc.mkdir()
    r = _prune(root, _stub(tmp_path, [made["version"]]), prefix=f"TL_PROC='{empty_proc}' ")
    assert "SKIPPED" in r.stdout and made["version"].is_dir()


def test_a_version_is_set_aside_before_it_is_deleted(tmp_path):
    """A run killed mid-rm leaves it under browsers/.retiring, which the decider
    always prints again; camoufox's own listing would never see it."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    r = _prune(root, _stub(tmp_path, [made["version"]]))
    assert "set aside" in r.stdout and not made["version"].exists()
    assert list((root / "browsers" / ".retiring").iterdir()) == []


def test_a_leftover_is_removed(tmp_path):
    root = tmp_path / "camoufox"
    _tree(root)
    left = root / "browsers" / ".retiring" / "official.146-aa.40417"
    (left / "sub").mkdir(parents=True)
    r = _prune(root, _stub(tmp_path, [left]))
    assert r.returncode == 0 and not left.exists()


def test_dot_components_and_camoufox_own_names_are_refused(tmp_path):
    root = tmp_path / "camoufox"
    made = _tree(root)
    (root / "config.json").write_text("{}")
    (root / "addons").mkdir()
    printed = [
        Path(f"{root}/browsers/../config.json"),
        Path(f"{root}/browsers/official/.."),
        root / "config.json",
        root / "addons",
    ]
    r = _prune(root, _stub(tmp_path, printed))
    assert r.stdout.count("refused") == 4
    assert (root / "config.json").exists() and (root / "addons").is_dir()
    assert made["repo"].is_dir()


def test_a_held_browser_stack_lock_skips(tmp_path):
    root = tmp_path / "camoufox"
    made = _tree(root)
    lock = tmp_path / "gh" / "locks" / "browser-stack.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _prune(root, _stub(tmp_path, [made["version"]]))
    assert "lock is held" in r.stdout and made["version"].is_dir()


def test_a_directory_in_a_dot_repo_is_refused(tmp_path):
    root = tmp_path / "camoufox"
    _tree(root)
    hidden = root / "browsers" / ".partial" / "v1"
    hidden.mkdir(parents=True)
    r = _prune(root, _stub(tmp_path, [hidden]))
    assert "refused" in r.stdout and hidden.is_dir()


def test_an_engine_launched_after_the_decision_is_spared(tmp_path):
    """The decision is a snapshot; each path is re-checked against /proc just
    before it is moved or removed."""
    import shutil
    import time

    root = tmp_path / "camoufox"
    made = _tree(root)
    binary = made["version"] / "camoufox-bin"
    shutil.copy2(shutil.which("sleep"), binary)
    proc = subprocess.Popen([str(binary), "30"])
    try:
        for _ in range(100):
            if Path(f"/proc/{proc.pid}/exe").exists():
                break
            time.sleep(0.05)
        r = _prune(root, _stub(tmp_path, [made["version"]]))
    finally:
        proc.kill()
        proc.wait()
    assert "spared" in r.stdout and binary.exists()


def _running(binary: Path):
    """A process executing ``binary`` (a copy of sleep), once /proc shows it."""
    import shutil
    import time

    shutil.copy2(shutil.which("sleep"), binary)
    proc = subprocess.Popen([str(binary), "30"])
    for _ in range(100):
        if Path(f"/proc/{proc.pid}/exe").exists():
            break
        time.sleep(0.05)
    return proc


def test_a_legacy_browser_started_after_the_decision_spares_the_whole_root(tmp_path):
    """The pre-0.5 engine runs from <root>/camoufox-bin, not from any file the
    decider printed, so the re-check covers every root entry against an exe
    anywhere in the root outside browsers/."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    version_json = root / "version.json"
    version_json.write_text("{}")
    proc = _running(root / "camoufox-bin")
    try:
        r = _prune(root, _stub(tmp_path, [version_json, made["legacy_file"], made["legacy_dir"]]))
    finally:
        proc.kill()
        proc.wait()
    assert version_json.exists() and made["legacy_file"].exists() and made["legacy_dir"].is_dir()
    assert r.stdout.count("spared") == 3


def test_an_engine_browser_does_not_spare_the_legacy_root(tmp_path):
    """A 0.5 engine running from browsers/ is not the root engine."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    proc = _running(made["version"] / "camoufox-bin")
    try:
        r = _prune(root, _stub(tmp_path, [made["legacy_file"]]))
    finally:
        proc.kill()
        proc.wait()
    assert not made["legacy_file"].exists(), r.stdout


def test_a_version_holding_a_mount_is_never_moved(tmp_path):
    """A rename would carry the mount to a path the mount snapshot does not name."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    mi = tmp_path / "mountinfo"
    mi.write_text(f"20 1 0:20 / / rw - x x rw\n21 1 0:21 / {made['deep']} rw - x x rw\n")
    r = _prune(root, _stub(tmp_path, [made["version"]]), prefix=f"export TL_MOUNTINFO='{mi}'\n")
    assert made["deep"].is_dir() and made["version"].is_dir(), r.stdout
    assert not (root / "browsers" / ".retiring").exists() or not any(
        (root / "browsers" / ".retiring").iterdir()
    )
    assert "spared" in r.stdout


def test_a_failed_legacy_entry_stops_the_rest_of_the_root(tmp_path):
    """precomplete is printed last and is the marker the next run finds the root
    by; once one root entry stays, nothing after it in the root goes."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    precomplete = root / "precomplete"
    precomplete.write_text("x")
    other = root / "omni.ja"
    other.write_text("x")
    mi = tmp_path / "mountinfo"
    mi.write_text(f"20 1 0:20 / / rw - x x rw\n21 1 0:21 / {made['legacy_dir']} rw - x x rw\n")
    r = _prune(
        root,
        _stub(tmp_path, [made["legacy_file"], made["legacy_dir"], other, precomplete]),
        prefix=f"export TL_MOUNTINFO='{mi}'\n",
    )
    assert not made["legacy_file"].exists()
    assert made["legacy_dir"].is_dir() and other.exists() and precomplete.exists(), r.stdout


def test_a_root_entry_spared_as_live_stops_the_rest_of_the_root(tmp_path):
    """A browser seen at the first entry and gone by the next still leaves the
    marker in place: the rule is "once one stays, the rest stay"."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    precomplete = root / "precomplete"
    precomplete.write_text("x")
    seen = tmp_path / "seen"
    once = f"_runs_from() {{ [ -e '{seen}' ] && return 1; : > '{seen}'; return 0; }}\n"
    r = _prune(root, _stub(tmp_path, [made["legacy_file"], precomplete]), prefix=once)
    assert made["legacy_file"].exists() and precomplete.exists(), r.stdout


def test_a_symlinked_retiring_dir_is_refused(tmp_path):
    """The move would follow the link out of the cache (and across filesystems copy
    the whole engine)."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "browsers" / ".retiring").symlink_to(outside)
    r = _prune(root, _stub(tmp_path, [made["version"]]))
    assert made["version"].is_dir() and list(outside.iterdir()) == [], r.stdout
    assert "refused" in r.stdout or "spared" in r.stdout


def test_a_browser_launched_during_the_move_keeps_the_set_aside_engine(tmp_path):
    """Checked again after the rename: past it, camoufox can no longer list or launch
    the version, so anything still running from it started before."""
    root = tmp_path / "camoufox"
    made = _tree(root)
    seen = tmp_path / "seen"
    later = f"_runs_from() {{ [ -e '{seen}' ] && return 0; : > '{seen}'; return 1; }}\n"
    r = _prune(root, _stub(tmp_path, [made["version"]]), prefix=later)
    retiring = list((root / "browsers" / ".retiring").iterdir())
    assert len(retiring) == 1 and retiring[0].is_dir(), r.stdout
    assert "spared" in r.stdout


def test_the_step_prunes_the_cache_secrets_env_names(tmp_path):
    """The provisioner installs where secrets.env's XDG_CACHE_HOME points, and this
    unit does not load that file: the step reads it by name, for its own run only."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = tmp_path / "elsewhere"
    (repo / "secrets.env").write_text(
        f'XDG_CACHE_HOME="{cache}"\nCAMOUFOX_EXECUTABLE_PATH=/opt/cf/camoufox-bin\nAPI_KEY=nope\n'
    )
    script = (
        f"source '{_HYGIENE}'\nREPO_DIR='{repo}'\nunset XDG_CACHE_HOME CAMOUFOX_EXECUTABLE_PATH\n"
        'prune_browser_engines() { echo "root=$1 xdg=$XDG_CACHE_HOME ov=${CAMOUFOX_EXECUTABLE_PATH-} key=${API_KEY-}"; }\n'
        "prune_camoufox_engines_step\n"
        'echo "after=${XDG_CACHE_HOME-unset}"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert f"root={cache}/camoufox xdg={cache} ov= key=" in r.stdout
    assert "after=unset" in r.stdout  # nothing leaks into the steps after it


def test_the_step_defaults_to_the_home_cache(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script = (
        f"source '{_HYGIENE}'\nREPO_DIR='{repo}'\nHOME='{tmp_path}'\nunset XDG_CACHE_HOME\n"
        'prune_browser_engines() { echo "root=$1"; }\n'
        "prune_camoufox_engines_step"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert f"root={tmp_path}/.cache/camoufox" in r.stdout, r.stdout + r.stderr


def test_the_step_reads_the_secrets_file_secrets_path_names(tmp_path):
    """SECRETS_PATH relocates secrets.env for the runtime and the provisioner; an
    override set there must reach the decider too, or its engine looks unused."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secrets.env").write_text("CAMOUFOX_EXECUTABLE_PATH=/wrong\n")
    elsewhere = tmp_path / "conf" / "secrets.env"
    elsewhere.parent.mkdir()
    elsewhere.write_text(f"XDG_CACHE_HOME={tmp_path / 'c'}\nCAMOUFOX_EXECUTABLE_PATH=/opt/cf/bin\n")
    script = (
        f"source '{_HYGIENE}'\nREPO_DIR='{repo}'\nexport SECRETS_PATH='{elsewhere}'\n"
        "unset XDG_CACHE_HOME CAMOUFOX_EXECUTABLE_PATH\n"
        'prune_browser_engines() { echo "root=$1"; }\n'
        "prune_camoufox_engines_step"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert f"root={tmp_path / 'c'}/camoufox" in r.stdout, r.stdout + r.stderr


def test_a_secrets_path_under_home_is_expanded(tmp_path):
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "secrets.env").write_text(f"XDG_CACHE_HOME={tmp_path / 'c'}\n")
    script = (
        f"source '{_HYGIENE}'\nREPO_DIR='{tmp_path}'\nHOME='{tmp_path}'\n"
        "export SECRETS_PATH='~/conf/secrets.env'\nunset XDG_CACHE_HOME\n"
        'prune_browser_engines() { echo "root=$1"; }\n'
        "prune_camoufox_engines_step"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert f"root={tmp_path / 'c'}/camoufox" in r.stdout, r.stdout + r.stderr


def test_the_store_knobs_still_come_from_the_checkout(tmp_path):
    """SECRETS_PATH is honoured for the browser settings only: the hook-audit
    stores' writers read their directories from the checkout's secrets.env."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secrets.env").write_text("GENESIS_MERGE_OVERRIDE_DIR=/from/repo\n")
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text("GENESIS_MERGE_OVERRIDE_DIR=/from/elsewhere\n")
    script = (
        f"source '{_HYGIENE}'\nREPO_DIR='{repo}'\nexport SECRETS_PATH='{elsewhere}'\n"
        '_load_store_knob GENESIS_MERGE_OVERRIDE_DIR\necho "dir=$GENESIS_MERGE_OVERRIDE_DIR"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert "dir=/from/repo" in r.stdout, r.stdout + r.stderr
