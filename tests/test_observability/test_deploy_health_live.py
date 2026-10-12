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
    _engine_json(w)
    return w


def _manifest(w, repo: str | None = None, text: str | None = None) -> None:
    """A manifest the predicate reads, and the engine stand-in agreeing with it
    (an empty manifest of this repository; `live` holds nothing listed)."""
    path = w.home / ".genesis" / "deploy_manifest.json"
    if text is not None:
        path.write_text(text)
        _engine_json(w, state="error", reason="the deploy manifest is malformed")
        return
    common = repo or _git(w.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    path.write_text(json.dumps({"version": 3, "repo": common, "candidates": []}))
    _engine_json(w, state="ok")


def _engine(w, body: str) -> None:
    """A stand-in engine entry (its real `list --json` output is pinned by
    tests/test_scripts/test_deploy_candidates_list_contract.py)."""
    entry = w.root / "scripts" / "deploy_candidates"
    entry.write_text("#!/bin/sh\n" + body + "\n")
    entry.chmod(0o755)


def _row(branch: str, head: str = "a" * 40, in_checkout: bool = False) -> dict:
    return {"branch": branch, "head": head, "in_checkout": in_checkout}


def _engine_json(
    w,
    *,
    state: str = "absent",
    reason: str | None = None,
    listed: list[dict] | None = None,
    holds: list[dict] | None = (),
    live_reason: str | None = None,
) -> None:
    """The stand-in prints this `list --json` reading (holds=None: unreadable)."""
    data = {
        "version": 1,
        "manifest": {"state": state, "reason": reason},
        "base": w.base,
        "listed": listed or [],
        "live": {
            "tip": None,
            "holds": None
            if holds is None
            else [{"branch": r["branch"], "head": r["head"]} for r in holds],
            "reason": live_reason,
        },
    }
    out = w.tmp / "engine.json"
    out.write_text(json.dumps(data))
    _engine(w, f"cat '{out}'")


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
    _engine_json(world, state="ok", listed=[_row("feat/c")], holds=[_row("feat/c")])
    live = dh.collect_live(world.root)
    assert live == {"state": "other", "candidates": 1}
    assert dh.live_findings(live) == ["live_off_branch:1"]


@pytest.mark.parametrize("state", ["absent", "ok", "foreign"])
def test_off_live_with_nothing_listed_is_quiet(world, state):
    """No manifest, an empty one, or another repository's (the predicate's
    `other`): nothing is meant to be live here, even with code in `live`."""
    _build_live(world, {"b.txt": "b\n"})
    _git(world.root, "switch", "-q", "main")
    _engine_json(world, state=state, holds=[_row("feat/c")])
    live = dh.collect_live(world.root)
    assert live["candidates"] == 0 and dh.live_findings(live) == []


@pytest.mark.parametrize(
    "body",
    [
        "echo 'ERROR: unexpected' >&2; exit 1",
        "echo 'something new'",
        "echo '1. feat/c  no PR  owner s  added t  pinned at abc'",
        "echo '{\"version\": 2}'",
        'echo \'{"version": 1, "manifest": {"state": "ok"}, "listed": [{"branch": "x"}]}\'',
    ],
)
def test_a_list_that_cannot_be_read_never_reads_as_zero(world, body):
    _build_live(world, {"b.txt": "b\n"})
    _git(world.root, "switch", "-q", "main")
    _engine(world, body)
    live = dh.collect_live(world.root)
    assert live["state"] == "other" and live["candidates"] is None
    assert dh.live_unknown(live) and dh.live_findings(live) == []


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
    assert live["state"] == "other" and live["candidates"] is None
    # The group was killed; reaping can lag a moment on a loaded box, so allow
    # it a bounded few seconds rather than racing the kernel (flaky under load).
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while not _pid_gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _pid_gone(pid)


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


# ── round 1: an unknown reading is never a healthy one ─────────────────────────


def test_on_live_an_unreadable_base_is_unreadable_not_healthy(world, monkeypatch):
    """Round-1 review: with the base unreadable, `live` read as healthy and
    upto=None suppressed every comparison, so the snapshot claimed what it could
    not establish."""
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    _git(world.root, "update-ref", "-d", "refs/remotes/origin/main")
    live = dh.collect_live(world.root)
    assert live["state"] == "unreadable" and live["on_live_branch"] is True
    assert "merge-base" in live["reason"]
    assert dh.live_findings(live) == ["live_unreadable"]
    monkeypatch.setattr(dh, "collect_main_checkout_dirty", lambda repo: {"status": "clean"})
    assert dh._collect_sync(world.root, world.home / ".genesis")["upto"] is None


def test_on_live_a_failed_tier2_diff_is_unreadable(world, monkeypatch):
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    real = dh._run_git

    def failing_diff(repo, *args, timeout):
        if args and args[0] == "diff":
            return 128, "", "fatal: bad object"
        return real(repo, *args, timeout=timeout)

    monkeypatch.setattr(dh, "_run_git", failing_diff)
    live = dh.collect_live(world.root)
    assert live["state"] == "unreadable" and live["base"] == world.base
    assert dh.live_findings(live) == ["live_unreadable"]


def test_a_failed_probe_on_live_keeps_the_comparisons_off_head(world, monkeypatch):
    """The unanswered predicate on `live` must still say it is on `live`, so the
    comparisons do not fall back to HEAD, where every candidate reads as drift."""
    _build_live(world, {"b.txt": "b\n"})
    monkeypatch.setattr(dh, "_run_probe", lambda argv, timeout: None)
    live = dh.collect_live(world.root)
    assert live["on_live_branch"] is True
    monkeypatch.setattr(dh, "collect_main_checkout_dirty", lambda repo: {"status": "clean"})
    assert dh._collect_sync(world.root, world.home / ".genesis")["upto"] is None


def test_off_live_an_unanswered_predicate_is_an_unknown_count():
    assert dh.live_unknown({"state": "other", "candidates": None, "reason": "x"})
    assert not dh.live_unknown({"state": "other", "candidates": 0})
    assert not dh.live_unknown({"state": "other"})


# ── round 3: every live fact from one engine reading ───────────────────────────


def test_off_live_a_listed_candidate_counts_with_no_live_ref(world):
    """Round-2 review: the first `add` writes the manifest and no `live` ref
    exists until a rebuild; the count no longer hinges on that ref."""
    _engine_json(world, state="ok", listed=[_row("feat/c")])
    live = dh.collect_live(world.root)
    assert live == {"state": "other", "candidates": 1}
    assert dh.live_findings(live) == ["live_off_branch:1"]


def test_off_live_a_candidate_the_checkout_already_has_is_not_counted(world):
    _engine_json(world, state="ok", listed=[_row("feat/old", in_checkout=True), _row("feat/c")])
    assert dh.collect_live(world.root)["candidates"] == 1


def test_off_live_a_broken_manifest_is_unreadable_and_compares_with_head(world, monkeypatch):
    """A manifest exists and the engine refuses it on every read: a finding with
    the engine's reason, never an unknown carried forever (round-2 audit D4)."""
    _engine_json(world, state="error", reason="the deploy manifest is malformed: x")
    live = dh.collect_live(world.root)
    assert live == {"state": "unreadable", "reason": "the deploy manifest is malformed: x"}
    assert dh.live_findings(live) == ["live_unreadable"]
    monkeypatch.setattr(dh, "collect_main_checkout_dirty", lambda repo: {"status": "clean"})
    assert dh._collect_sync(world.root, world.home / ".genesis")["upto"] == "HEAD"


def test_on_live_a_listed_candidate_live_does_not_hold_is_unbuilt(world):
    """Round-2 review (P1): added since the last rebuild, or left out of it."""
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    _engine_json(
        world,
        state="ok",
        listed=[_row("feat/c"), _row("feat/d", "d" * 40), _row("feat/old", "e" * 40, True)],
        holds=[_row("feat/c")],
    )
    live = dh.collect_live(world.root)
    assert live["unbuilt"] == 1 and live["unlisted"] == 0
    assert dh.live_findings(live) == ["live_unbuilt:1"]


def test_on_live_a_repinned_candidate_is_unbuilt_not_unlisted(world):
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    _engine_json(world, state="ok", listed=[_row("feat/c", "f" * 40)], holds=[_row("feat/c")])
    live = dh.collect_live(world.root)
    assert (live["unbuilt"], live["unlisted"]) == (1, 0)


def test_on_live_a_held_candidate_nothing_lists_is_unlisted(world):
    """Dropped without a rebuild: its code stays in `live`."""
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    _engine_json(world, state="ok", listed=[], holds=[_row("feat/c")])
    live = dh.collect_live(world.root)
    assert dh.live_findings(live) == ["live_unlisted:1"]


@pytest.mark.parametrize(
    "reading",
    [
        {"state": "error", "reason": "the deploy manifest is malformed: no candidates"},
        {"state": "ok", "holds": None, "live_reason": "cannot read `live`"},
        None,
    ],
    ids=["manifest-error", "holds-unreadable", "no-answer"],
)
def test_on_live_an_engine_that_cannot_compare_is_unreadable(world, reading):
    """Round-3 design review: the predicate checks only the manifest's repo key;
    the engine can still refuse it, and that must not read as `live_unlisted`."""
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    if reading is None:
        _engine(world, "exit 1")
    else:
        _engine_json(world, **reading)
    live = dh.collect_live(world.root)
    assert live["state"] == "unreadable" and live["on_live_branch"] is True
    assert live["base"] == world.base
    assert dh.live_findings(live) == ["live_unreadable"]


def test_on_the_live_branch_with_no_manifest_the_held_code_is_unlisted(world, monkeypatch):
    """Round-2 review: HEAD is the branch `live` but the manifest is gone; the
    predicate says `other`. Candidate code is checked out: say so, and compare
    with the base, never HEAD."""
    _build_live(world, {"b.txt": "b\n"})
    _engine_json(world, state="absent", holds=[_row("feat/c")])
    live = dh.collect_live(world.root)
    assert live == {"state": "unbound", "on_live_branch": True, "base": world.base, "unlisted": 1}
    assert dh.live_findings(live) == ["live_unbound", "live_unlisted:1"]
    monkeypatch.setattr(dh, "collect_main_checkout_dirty", lambda repo: {"status": "clean"})
    assert dh._collect_sync(world.root, world.home / ".genesis")["upto"] == world.base


def test_on_the_live_branch_with_no_manifest_and_no_reading_is_unreadable(world):
    _build_live(world, {"b.txt": "b\n"})
    _engine(world, "exit 1")
    live = dh.collect_live(world.root)
    assert live["state"] == "unreadable" and live["on_live_branch"] is True
    assert dh.live_findings(live) == ["live_unreadable"]


def test_the_real_engine_and_predicate_after_an_add_without_a_rebuild(dc, dc_ready, monkeypatch):
    """End to end on the real engine entry: rebuild `live` with one candidate,
    add a second without rebuilding, and the snapshot names it unbuilt; drop the
    first without a rebuild, and `live` holds code nothing lists."""
    from genesis import env

    w = dc_ready
    monkeypatch.setenv("HOME", str(w.home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(env, "update_in_progress", lambda: False)
    w.install_engine()  # the entry, the engine modules and the marker lib it sources
    shutil.copy2(PREDICATE, w.root / "scripts" / "lib" / "live_checkout.py")
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    live = dh.collect_live(w.root)
    assert live["state"] == "live" and dh.live_findings(live) == [], live
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert dh.live_findings(dh.collect_live(w.root)) == ["live_unbuilt:1"]
    w.write_manifest([w.entry("feat/b")])
    assert dh.live_findings(dh.collect_live(w.root)) == ["live_unbuilt:1", "live_unlisted:1"]


def test_on_live_a_candidate_repinned_backwards_is_unbuilt(world):
    """Round-3 premise check: HEAD contains an older pin of a merged branch, yet
    `live` holds a later head nobody pinned; containment must not hide that."""
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    _engine_json(
        world,
        state="ok",
        listed=[_row("feat/c", "0" * 40, in_checkout=True)],
        holds=[_row("feat/c", "1" * 40)],
    )
    assert dh.live_findings(dh.collect_live(world.root)) == ["live_unbuilt:1"]


def test_on_live_a_contained_candidate_no_rebuild_merged_is_built(world):
    _build_live(world, {"b.txt": "b\n"})
    _manifest(world)
    _engine_json(world, state="ok", listed=[_row("feat/old", in_checkout=True)], holds=[])
    assert dh.live_findings(dh.collect_live(world.root)) == []


def test_the_real_engine_names_a_backwards_repin_unbuilt(dc, dc_ready, monkeypatch):
    from genesis import env

    w = dc_ready
    monkeypatch.setenv("HOME", str(w.home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(env, "update_in_progress", lambda: False)
    w.install_engine()
    shutil.copy2(PREDICATE, w.root / "scripts" / "lib" / "live_checkout.py")
    first = w.candidate("feat/a", {"x.txt": "x\n"})
    w.candidate("feat/a", {"x.txt": "x2\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    assert dh.live_findings(dh.collect_live(w.root)) == []
    w.write_manifest([w.entry("feat/a", head=first)])
    assert dh.live_findings(dh.collect_live(w.root)) == ["live_unbuilt:1"]


def test_an_unbound_checkout_holding_nothing_is_still_a_finding(world):
    """Round-4 review: HEAD on `live` with no manifest and nothing held read as
    healthy, yet the deploy scripts refuse that checkout (restart included)."""
    _build_live(world, {"b.txt": "b\n"})
    _engine_json(world, state="absent", holds=[])
    live = dh.collect_live(world.root)
    assert live["state"] == "unbound" and live["unlisted"] == 0
    assert dh.live_findings(live) == ["live_unbound"]


def test_two_merge_bases_give_a_base_holding_both(world, monkeypatch):
    """Round-4 review: two candidates merged into `live`, then merged separately
    into origin/main, give HEAD and origin/main two merge bases; one picked alone
    omitted the other's files from the tier-2 and Guardian comparisons."""
    base = world.base
    for branch, files in (
        ("feat/a", {"scripts/update.sh": "#!/bin/sh\n# a\n"}),
        ("feat/b", {"src/genesis/guardian/x.py": "X = 1\n"}),
    ):
        _git(world.root, "switch", "-q", "-c", branch, base)
        _commit(world.root, files, branch)
    _git(world.root, "switch", "-q", "--detach", base)
    _git(world.root, "merge", "-q", "--no-ff", "-m", "rebuild a", "feat/a")
    _git(world.root, "merge", "-q", "--no-ff", "-m", "rebuild b", "feat/b")
    _git(world.root, "switch", "-q", "-C", "live", _git(world.root, "rev-parse", "HEAD"))
    # origin/main merges the two candidates separately, after the rebuild.
    _git(world.origin, "fetch", "-q", str(world.root), "feat/a:feat/a", "feat/b:feat/b")
    _git(world.origin, "merge", "-q", "--no-ff", "-m", "up a", "feat/a")
    _git(world.origin, "merge", "-q", "--no-ff", "-m", "up b", "feat/b")
    _git(world.root, "fetch", "-q", "origin")
    bases = _git(world.root, "merge-base", "--all", "HEAD", "origin/main").split()
    assert len(bases) == 2, "precondition: two merge bases"
    got = dh._live_base(world.root)
    assert got not in bases, "one base picked arbitrarily"
    assert dh.collect_tier2_pending(world.root, base[:9], got) == ["scripts/update.sh"]
    since = base[:9]
    state = world.tmp / "host.json"
    state.write_text(json.dumps({"version": {"deployed_commit": since}}))
    assert dh.collect_host_gateway(world.root, state, upto=got)["status"] == "drift"
    for single in bases:  # the defect: one base alone loses a candidate's files
        assert (
            dh.collect_tier2_pending(world.root, since, single) == []
            or dh.collect_host_gateway(world.root, state, upto=single)["status"] == "ok"
        )
