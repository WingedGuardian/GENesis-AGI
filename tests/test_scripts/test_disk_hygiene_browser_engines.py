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
    left = root / "browsers" / ".retiring" / "official.146-aa.123"
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
