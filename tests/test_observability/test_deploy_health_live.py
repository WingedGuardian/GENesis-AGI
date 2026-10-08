"""deploy_health on `live`, the integration branch scripts/deploy_candidates
rebuilds from the deploy manifest (#2978 PR E).

On `live` the behind-count, tier-2 pending and host drift are measured from the
commit `live` was built on, never HEAD, so a candidate's own files never read as
undeployed merges; and three non-paging findings say when `live` cannot be read,
when the checkout has left it while candidates are listed, and when `live`
carries a candidate's update.sh-only files. Real throwaway repos and the real
predicate script: the collector shells out to both.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

dh = importlib.import_module("genesis.observability.snapshots.deploy_health")

REPO = Path(__file__).resolve().parents[2]
PREDICATE = REPO / "scripts" / "lib" / "live_checkout.py"
ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**ENV, "HOME": str(repo)},
    )
    return out.stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        _git(repo, "add", rel)
    _git(repo, "commit", "-qm", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A deploy checkout cloned from origin, with the real predicate, a fake
    engine entry, and HOME (where the manifest lives) pointed at tmp."""
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    from genesis import env

    monkeypatch.setattr(env, "update_in_progress", lambda: False)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, {"a.txt": "1\n", "scripts/update.sh": "#!/bin/sh\n"}, "c1")
    root = tmp_path / "root"
    _git(tmp_path, "clone", "-q", str(origin), str(root))
    (root / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
    shutil.copy(PREDICATE, root / "scripts" / "lib" / "live_checkout.py")

    class W:
        pass

    w = W()
    w.tmp, w.home, w.origin, w.root = tmp_path, home, origin, root
    w.base = _git(root, "rev-parse", "origin/main")
    return w


def _manifest(w, repo: str | None = None, text: str | None = None) -> None:
    path = w.home / ".genesis" / "deploy_manifest.json"
    if text is not None:
        path.write_text(text)
        return
    common = repo or _git(w.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    path.write_text(json.dumps({"version": 3, "repo": common, "candidates": []}))


def _engine(w, body: str) -> None:
    """A stand-in engine entry (its real `list` output is pinned by
    tests/test_scripts/test_deploy_candidates_list_contract.py)."""
    entry = w.root / "scripts" / "deploy_candidates"
    entry.write_text("#!/bin/sh\n" + body + "\n")
    entry.chmod(0o755)


def _build_live(w, files: dict[str, str]) -> str:
    """`live` = origin/main + one candidate merged on top, built from a commit
    (no upstream), as the engine does; the checkout is left on it."""
    _git(w.root, "switch", "-q", "-c", "feat/c")
    _commit(w.root, files, "candidate")
    _git(w.root, "switch", "-q", "--detach", w.base)
    _git(w.root, "merge", "-q", "--no-ff", "-m", "rebuild", "feat/c")
    tip = _git(w.root, "rev-parse", "HEAD")
    _git(w.root, "switch", "-q", "-C", "live", tip)
    return tip


def _advance_origin(w, n: int) -> None:
    for i in range(n):
        _commit(w.origin, {"a.txt": f"{i + 2}\n"}, f"m{i}")
    _git(w.root, "fetch", "-q", "origin")


# ── the predicate and the engine ───────────────────────────────────────────


def test_on_main_with_no_live_branch_nothing_changes(world):
    live = dh.collect_live(world.root)
    assert live == {"state": "other", "candidates": 0}
    assert dh.live_findings(live) == []
    facts = dh.collect_git_facts(world.root)
    assert facts["commits_behind_upstream"] == 0


def test_on_live_the_base_and_the_candidate_tier2_are_read(world):
    _build_live(world, {"scripts/update.sh": "#!/bin/sh\n# candidate\n"})
    _manifest(world)
    live = dh.collect_live(world.root)
    assert live["state"] == "live"
    assert live["base"] == world.base
    assert live["candidate_tier2"] == 1
    assert dh.live_findings(live) == ["live_candidate_tier2:1"]


def test_on_live_the_behind_count_reads_origin_main(world):
    """The engine's `switch -C live <sha>` sets no upstream, so @{upstream}
    fails on `live`; the count is origin/main's commits since the base."""
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    _advance_origin(world, 2)
    assert dh.collect_git_facts(world.root)["commits_behind_upstream"] is None
    assert dh.collect_git_facts(world.root, on_live=True)["commits_behind_upstream"] == 2


def test_on_live_a_candidates_tier2_and_guardian_files_are_not_drift(world, tmp_path):
    _build_live(
        world,
        {"scripts/update.sh": "#!/bin/sh\n# c\n", "src/genesis/env.py": "X = 1\n"},
    )
    _manifest(world)
    live = dh.collect_live(world.root)
    since = world.base[:9]
    assert dh.collect_tier2_pending(world.root, since) == ["scripts/update.sh"]
    assert dh.collect_tier2_pending(world.root, since, live["base"]) == []
    state = tmp_path / "host.json"
    state.write_text(json.dumps({"version": {"deployed_commit": since}}))
    assert dh.collect_host_gateway(world.root, state)["status"] == "drift"
    assert dh.collect_host_gateway(world.root, state, upto=live["base"])["status"] == "ok"
    assert dh.collect_host_gateway(world.root, state, upto=None)["status"] == "no_data"
    assert dh.collect_tier2_pending(world.root, since, None) is None


def test_a_malformed_manifest_on_live_is_unreadable(world):
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world, text="{not json")
    live = dh.collect_live(world.root)
    assert live["state"] == "unreadable"
    assert dh.live_findings(live) == ["live_unreadable"]


def test_off_live_with_candidates_listed_is_off_branch(world):
    _build_live(world, {"b.txt": "b\n"})
    _git(world.root, "switch", "-q", "main")
    _engine(world, "echo '1. feat/c  no PR  owner s  added t  pinned at abc'")
    live = dh.collect_live(world.root)
    assert live == {"state": "other", "candidates": 1}
    assert dh.live_findings(live) == ["live_off_branch:1"]


@pytest.mark.parametrize(
    "body",
    [
        "echo 'No deploy manifest (/x): nothing is meant to be live.'",
        "echo 'The deploy manifest lists no candidates.'",
    ],
)
def test_off_live_with_nothing_listed_is_quiet(world, body):
    _build_live(world, {"b.txt": "b\n"})
    _git(world.root, "switch", "-q", "main")
    _engine(world, body)
    assert dh.collect_live(world.root)["candidates"] == 0


@pytest.mark.parametrize(
    "body",
    [
        "echo 'ERROR: the deploy manifest is malformed' >&2; exit 1",
        "echo 'something new'",
        "echo '1. feat/c'; echo 'trailing prose'",
    ],
)
def test_a_list_that_cannot_be_read_never_reads_as_zero(world, body):
    _build_live(world, {"b.txt": "b\n"})
    _git(world.root, "switch", "-q", "main")
    _engine(world, body)
    live = dh.collect_live(world.root)
    assert live == {"state": "other", "candidates": None}
    assert dh.live_findings(live) == []


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_hung_list_times_out_with_no_key_and_no_survivor(world):
    """The whole process group goes: the shell's own child (`sleep`) is what a
    kill of the shell alone would leave running."""
    _build_live(world, {"b.txt": "b\n"})
    _git(world.root, "switch", "-q", "main")
    pidfile = world.tmp / "pid"
    _engine(world, f"sleep 30 & echo $! > {pidfile}; wait")
    start = time.monotonic()
    live = dh.collect_live(world.root, timeout=2)
    assert time.monotonic() - start < 15
    assert live == {"state": "other", "candidates": None}
    assert _pid_gone(int(pidfile.read_text()))


def test_during_a_deploy_live_is_not_probed_but_the_branch_is_known(world, monkeypatch):
    _build_live(world, {"b.txt": "b\n"})
    from genesis import env

    monkeypatch.setattr(env, "update_in_progress", lambda: True)
    live = dh.collect_live(world.root)
    assert live == {"state": "deploying", "on_live_branch": True, "base": world.base}
    assert dh.live_findings(live) == []
    monkeypatch.setattr(dh, "collect_main_checkout_dirty", lambda repo: {"status": "deploying"})
    assert dh._collect_sync(world.root, world.home / ".genesis")["upto"] == world.base


def test_a_failed_probe_off_live_raises_nothing(world, monkeypatch):
    """A slow or broken predicate on an install that does not run `live` must
    not turn its snapshot to attention."""
    monkeypatch.setattr(dh, "_run_probe", lambda argv, timeout: None)
    live = dh.collect_live(world.root)
    assert live["state"] == "other" and "did not answer" in live["reason"]
    assert dh.live_findings(live) == []


def test_a_failed_probe_on_live_is_unreadable(world, monkeypatch):
    _build_live(world, {"b.txt": "b\n"})
    monkeypatch.setattr(dh, "_run_probe", lambda argv, timeout: None)
    assert dh.live_findings(dh.collect_live(world.root)) == ["live_unreadable"]


def test_a_linked_worktree_reports_no_live_keys(world, monkeypatch):
    """A worktree's snapshot (a session's MCP) must not report the deploy
    checkout's candidates: the probe is not even run there."""
    monkeypatch.setattr(
        dh, "collect_main_checkout_dirty", lambda repo: {"status": "not_deploy_root"}
    )

    def boom(repo, **kw):
        raise AssertionError("collect_live ran outside the deploy root")

    monkeypatch.setattr(dh, "collect_live", boom)
    got = dh._collect_sync(world.root, world.home / ".genesis")
    assert got["live"] is None and got["upto"] == "HEAD"


def test_on_live_the_snapshot_compares_with_the_base(world, monkeypatch):
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    monkeypatch.setattr(dh, "collect_main_checkout_dirty", lambda repo: {"status": "clean"})
    got = dh._collect_sync(world.root, world.home / ".genesis")
    assert got["live"]["state"] == "live"
    assert got["upto"] == world.base
    assert got["git"]["live"] == "live"


def test_derive_findings_carries_the_live_keys_after_the_others():
    found = dh.derive_findings(
        missing_units=None,
        tier2_pending=None,
        host_gateway={"status": "ok"},
        commits_behind=0,
        live={"state": "other", "candidates": 2},
    )
    assert found == ["live_off_branch:2"]
    assert (
        dh.derive_findings(
            missing_units=None, tier2_pending=None, host_gateway={}, commits_behind=0
        )
        == []
    )
