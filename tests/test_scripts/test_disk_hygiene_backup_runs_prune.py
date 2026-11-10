"""Tests for prune_backup_runs in scripts/disk_hygiene.sh: the retention prune of
~/.genesis/premerge-backups (45 days) and ~/.genesis/deploy-backups (90 days).

Sources the script (main is guarded) and calls the function against a scratch
directory, the source-and-call pattern of test_disk_hygiene_guard_corpus_prune.
"""

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _age(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def _prune(path: Path, days: int = 90) -> subprocess.CompletedProcess:
    # Paths travel as positional parameters, never as script text.
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"\nprune_backup_runs "$2" deploy-backups "$3"',
            "x",
            str(_HYGIENE),
            str(path),
            str(days),
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


def _run_dir(parent: Path, name: str, days: float) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / "genesis-server.service").write_text("[Service]\n")
    _age(d, days)
    return d


def test_an_old_run_goes_and_a_young_one_stays(tmp_path):
    root = tmp_path / "deploy-backups"
    old = _run_dir(root, "20260101T000000Z", 120)
    young = _run_dir(root, "20261001T000000Z", 10)
    r = _prune(root)
    assert not old.exists(), r.stdout
    assert young.exists()
    assert "failed or spared" not in r.stdout


def test_a_symlinked_backup_dir_is_never_pruned_through(tmp_path):
    """A symlink points at a directory Genesis does not own; every old directory
    there would be deleted (#3138 review)."""
    elsewhere = tmp_path / "shared-archive"
    unrelated = _run_dir(elsewhere, "2025-photos", 400)
    link = tmp_path / "deploy-backups"
    link.symlink_to(elsewhere)
    r = _prune(link)
    assert unrelated.exists()
    assert "prune SKIPPED" in r.stdout and "is a symlink" in r.stdout


def test_a_missing_backup_dir_is_a_quiet_no_op(tmp_path):
    r = _prune(tmp_path / "absent")
    assert r.stdout == ""


# ── live-move repair reports are kept while unresolved (#4331) ────────────
#
# A report folder holds the only saved copy of edits a live-move repair moved
# out of the checkout. The 90-day prune must not take it while the report is
# neither restored nor dismissed: deploy health would drop its alert with it.

import json  # noqa: E402

_KEEP = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "repair_report_keep.py"
_FOREIGN = {"path": "a.txt", "class": "foreign"}


def _report(root: Path, name: str, days: float, report) -> Path:
    d = root / name
    (d / "tree").mkdir(parents=True)
    (d / "tree" / "a.txt").write_text("saved edit\n")
    if report is not None:
        raw = report if isinstance(report, bytes) else json.dumps(report).encode()
        (d / "REPAIR.json").write_bytes(raw)
    for p in (d / "tree" / "a.txt", d / "tree", d / "REPAIR.json", d):
        if p.exists():
            _age(p, days)
    return d


_TS = "20260101T000000Z-4242-live-move-repair"
#: The deploy-backups call exactly as main() makes it (pinned below).
_MAIN_CALL = 'prune_backup_runs "$HOME/.genesis/deploy-backups" deploy-backups 90 keep_repair_report'


def test_main_prunes_deploy_backups_with_the_report_keep_rule():
    assert _MAIN_CALL in _HYGIENE.read_text()


def _prune_main(root: Path) -> subprocess.CompletedProcess:
    """main()'s deploy-backups prune, with $HOME/.genesis/deploy-backups = root."""
    home = root.parent.parent
    assert root == home / ".genesis" / "deploy-backups"
    return subprocess.run(
        ["bash", "-c", f'source "$1"\n{_MAIN_CALL}', "x", str(_HYGIENE)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
        env={**os.environ, "HOME": str(home)},
    )  # fmt: skip


def test_an_old_unresolved_report_is_kept_and_named(tmp_path):
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    d = _report(root, _TS, 120, {"repo": "/x", "paths": [_FOREIGN]})
    r = _prune_main(root)
    assert d.exists(), r.stdout
    assert f"keeping {d}" in r.stdout and "1 saved path" in r.stdout, r.stdout


def test_a_held_restore_keeps_the_report(tmp_path):
    """A restore that refused a part (a held aside) wrote no restored_at."""
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    held = {"inflight": None, "refused": 1, "restored_at": None}
    d = _report(root, _TS, 120, {"repo": "/x", "paths": [_FOREIGN], "restore": held})
    _prune_main(root)
    assert d.exists()


def test_a_restored_report_is_pruned_at_ninety_days(tmp_path):
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    done = {"inflight": None, "refused": 0, "restored_at": "2026-01-02T00:00:00Z"}
    d = _report(root, _TS, 120, {"repo": "/x", "paths": [_FOREIGN], "restore": done})
    young = _report(root, "20261001T000000Z-1-live-move-repair", 10, {"repo": "/x", "paths": [_FOREIGN], "restore": done})
    _prune_main(root)
    assert not d.exists() and young.exists()


def test_a_dismissed_report_is_pruned_at_ninety_days(tmp_path):
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    d = _report(root, _TS, 120, {"repo": "/x", "paths": [_FOREIGN], "dismissed_at": "2026-01-03T00:00:00Z"})
    _prune_main(root)
    assert not d.exists()


def test_a_report_with_nothing_saved_is_pruned(tmp_path):
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    d = _report(root, _TS, 120, {"repo": "/x", "paths": [{"path": "b", "class": "engine"}]})
    _prune_main(root)
    assert not d.exists()


def test_an_unreadable_report_is_kept_and_named(tmp_path):
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    bad = _report(root, _TS, 120, b"{not json")
    big = _report(root, "20260101T000001Z-1-live-move-repair", 120, b" " * (4 * 1024 * 1024 + 1))
    r = _prune_main(root)
    assert bad.exists() and big.exists(), r.stdout
    assert r.stdout.count("keeping ") == 2, r.stdout


def test_a_report_folder_with_no_report_yet_prunes_as_before(tmp_path):
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    d = _report(root, _TS, 120, None)
    _prune_main(root)
    assert not d.exists()


def test_premerge_backups_never_consult_a_report(tmp_path):
    """The keep rule is the deploy-backups call's alone; premerge-backups
    holds no repair reports and prunes exactly as before."""
    root = tmp_path / "premerge-backups"
    d = _report(root, _TS, 60, {"repo": "/x", "paths": [_FOREIGN]})
    subprocess.run(
        ["bash", "-c", 'source "$1"\nprune_backup_runs "$2" premerge-backups 45', "x", str(_HYGIENE), str(root)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
    )  # fmt: skip
    assert not d.exists()


def test_the_cleared_rule_matches_deploy_health(tmp_path):
    """The prune keeps a report exactly while deploy health would still count
    its saved paths: not dismissed, not cleared (_restore_counts), and at least
    one foreign row."""
    import importlib
    import importlib.util

    deploy_health = importlib.import_module("genesis.observability.snapshots.deploy_health")

    spec = importlib.util.spec_from_file_location("_keep", _KEEP)
    keep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(keep)
    blocks = [
        None, {}, "x", {"refused": 0}, {"refused": 0, "restored_at": "t"}, {"refused": 1, "restored_at": "t"},
        {"refused": True, "restored_at": "t"}, {"refused": 0, "restored_at": None}, {"refused": -1, "restored_at": "t"},
    ]  # fmt: skip
    for block in blocks:
        for dismissed in (None, "", "2026-01-03T00:00:00Z"):
            report = {"paths": [_FOREIGN], "restore": block, "dismissed_at": dismissed}
            _, cleared = deploy_health._restore_counts(block)
            want = not (bool(dismissed) or cleared)
            assert (keep.reason(report) is not None) == want, (block, dismissed)


def test_the_run_folder_a_held_restore_points_at_is_kept(tmp_path):
    """A held restore's newer content sits in its run folder's aside/, the
    only copy of it; the report names that run in restore.inflight (#4331
    review SF-1). A run folder no unresolved report names prunes as before."""
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    run, other = "20260102T000000Z-77-live-move-restore", "20260102T000001Z-78-live-move-restore"
    held = {"inflight": [run, "a.txt", "node"], "refused": 1, "restored_at": None}
    _report(root, _TS, 120, {"repo": "/x", "paths": [_FOREIGN], "restore": held})
    for name in (run, other):
        (root / name / "aside").mkdir(parents=True)
        (root / name / "aside" / "a.txt").write_text("newer content\n")
        _age(root / name / "aside" / "a.txt", 120)
        _age(root / name / "aside", 120)
        _age(root / name, 120)
    r = _prune_main(root)
    assert (root / run).exists(), r.stdout
    assert f"keeping {root / run} (the in-flight aside of the unresolved report {_TS})" in r.stdout
    assert not (root / other).exists(), r.stdout


def test_a_reader_that_crashes_keeps_the_folder(tmp_path):
    """Only the reader's own answer (exit 3) lets a folder go: a crash exits 1
    (#4331 review SF-2), and python3 missing exits 127."""
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    d = _report(root, _TS, 120, {"repo": "/x", "paths": [{"path": "b", "class": "engine"}]})
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python3").write_text("#!/bin/sh\necho Traceback >&2\nexit 1\n")
    (shim / "python3").chmod(0o755)
    home = root.parent.parent
    r = subprocess.run(
        ["bash", "-c", f'source "$1"\nPATH="$2:$PATH"\n{_MAIN_CALL}', "x", str(_HYGIENE), str(shim)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
        env={**os.environ, "HOME": str(home)},
    )  # fmt: skip
    assert d.exists(), r.stdout
    assert "could not be judged (exit 1)" in r.stdout, r.stdout
    _prune_main(root)
    assert not d.exists(), "control: the real reader lets a report with nothing saved go"


def test_a_dismissed_unreadable_report_is_pruned_at_ninety_days(tmp_path):
    """--dismiss TS on a report no reader can read writes DISMISSED.json; the
    prune then lets it go like any dismissed report, and its run folder."""
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    d = _report(root, _TS, 120, b"{not json")
    (d / "DISMISSED.json").write_text('{"dismissed_at": "2026-01-03T00:00:00Z", "unread": "not JSON"}')
    _age(d / "DISMISSED.json", 120)
    _age(d, 120)
    r = _prune_main(root)
    assert not d.exists(), r.stdout


def test_a_restore_run_named_for_its_report_lives_as_long_as_the_report(tmp_path):
    """#4011: a run folder carries its report's ts, so it is kept while that
    report's folder exists (the report itself is kept while unresolved), and
    pruned as before once the report is gone."""
    root = tmp_path / "home" / ".genesis" / "deploy-backups"
    ts = _TS[: -len("-live-move-repair")]
    report = _report(root, _TS, 10, {"repo": "/x", "paths": [_FOREIGN]})
    run = root / f"20260102T000000Z-77-{ts}-live-move-restore"
    orphan = root / "20260102T000000Z-78-20250101T000000Z-1-live-move-restore"
    for d in (run, orphan):
        (d / "aside").mkdir(parents=True)
        (d / "aside" / "a.txt").write_text("newer content a held restore did not write\n")
        _age(d / "aside" / "a.txt", 120)
        _age(d / "aside", 120)
        _age(d, 120)
    r = _prune_main(root)
    assert run.exists() and report.exists(), r.stdout
    assert f"keeping {run} (a restore run of the report {_TS}, kept while it is)" in r.stdout
    assert not orphan.exists(), "a run whose report is gone prunes as before"
