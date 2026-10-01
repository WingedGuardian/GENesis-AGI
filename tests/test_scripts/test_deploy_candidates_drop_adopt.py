"""deploy_candidates: drop, adopt and status; admission and its completeness
locks; the shell entry.

The scratch world is tests/test_scripts/_deploy_candidates_world.py.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess

import pytest

from tests.test_scripts._deploy_candidates_world import (
    ENGINE_FILES,
    ENTRY,
    HELPER_NAMES,
    HOOK_NAMES,
    REPO,
    SCRIPTS,
    World,
)

# ── drop ───────────────────────────────────────────────────────────────────


def _two_live(w: World, dc):
    w.candidate("feat/a", {"x.txt": "x\n"})
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    return hb


def test_drop_removes_the_candidate_from_live(dc, dc_ready, capsys):
    w = dc_ready
    hb = _two_live(w, dc)
    assert w.run(dc, "drop", "feat/a") == 0
    assert [e["branch"] for e in w.manifest()["candidates"]] == ["feat/b"]
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.root / "x.txt").exists()
    capsys.readouterr()
    assert w.run(dc, "drop", "feat/zzz") == 1
    assert "not a candidate" in capsys.readouterr().err


@pytest.mark.parametrize("origin", ["reachable", "unreachable"])
def test_drop_works_offline_and_never_admits_anything(dc, dc_ready, capsys, origin):
    """No GitHub, no fetch: drop only subtracts from what is live,
    on the base `live` already sits on. With origin REACHABLE and its main moved,
    a fetch would succeed and advance origin/main, so this arm is what proves no
    fetch ran; the unreachable arm proves drop does not need the network."""
    w = dc_ready
    hb = _two_live(w, dc)
    base_before = w.rev("refs/remotes/origin/main")
    w.candidate("feat/new", {"n.txt": "n\n"})
    data = w.manifest()
    data["candidates"].append(w.entry("feat/new"))
    w.write_manifest(data["candidates"])  # listed, but never rebuilt into live
    w.advance_main({"m.txt": "main moved\n"})
    if origin == "unreachable":
        w.git(w.root, "remote", "set-url", "origin", str(w.tmp / "nowhere.git"))
    w.gh_fail.update(range(1000))
    w.serving_sha = None
    assert w.run(dc, "drop", "feat/a") == 0, capsys.readouterr()
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.root / "n.txt").exists() and not (w.root / "m.txt").exists()
    assert w.rev("refs/remotes/origin/main") == base_before
    assert w.gh_calls == []


def test_drop_takes_out_what_carries_the_dropped_code_and_says_it_returns(dc, dc_ready, capsys):
    w = dc_ready
    a1 = w.candidate("feat/a", {"p.txt": "a's code\n"})
    w.candidate("feat/a", {"a2.txt": "a2\n"})
    w.candidate("feat/c", {"z.txt": "z\n"}, base=a1)
    hb = w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/c"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0
    capsys.readouterr()
    assert w.run(dc, "drop", "feat/a") == 0
    out = capsys.readouterr().out
    assert re.search(r"EXCLUDED: feat/c .*derived from dropped feat/a", out), out
    assert "come back at the next rebuild" in out
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.root / "p.txt").exists()


def test_a_drop_that_cannot_move_the_checkout_changes_nothing(dc, dc_ready, capsys):
    """Dropping a candidate that deleted a file brings the file back: an
    untracked file in its way refuses the drop BEFORE the manifest changes."""
    w = dc_ready
    w.candidate("feat/a", {"b.txt": None})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    assert not (w.root / "b.txt").exists()
    (w.root / "b.txt").write_text("untracked, in the way\n")
    before = w.manifest_path.read_text()
    tip = w.rev("refs/heads/live")
    capsys.readouterr()
    assert w.run(dc, "drop", "feat/a") == 1
    assert "in the way" in capsys.readouterr().err
    assert w.manifest_path.read_text() == before and w.rev("refs/heads/live") == tip
    assert (w.root / "b.txt").read_text() == "untracked, in the way\n"


def test_a_drop_on_a_dirty_checkout_changes_nothing(dc, dc_ready, capsys):
    w = dc_ready
    _two_live(w, dc)
    (w.root / "b.txt").write_text("edited in place\n")
    before = w.manifest_path.read_text()
    assert w.run(dc, "drop", "feat/a") == 1
    assert "b.txt" in capsys.readouterr().err
    assert w.manifest_path.read_text() == before


def test_drop_without_a_manifest_writes_nothing(dc, dc_world, capsys):
    w = dc_world
    assert w.run(dc, "drop", "feat/a") == 1
    assert "not a candidate" in capsys.readouterr().err
    assert sorted(p.name for p in (w.home / ".genesis").iterdir()) == ["locks"]


def test_drop_off_live_names_a_candidate_stacked_on_the_dropped_one(dc, dc_ready, capsys):
    w = dc_ready
    a1 = w.candidate("feat/a", {"p.txt": "p\n"})
    w.candidate("feat/c", {"z.txt": "z\n"}, base=a1)
    w.candidate("feat/d", {"q.txt": "q\n"}, base=a1)
    w.write_manifest([w.entry("feat/a"), w.entry("feat/c"), w.entry("feat/d")])
    assert w.run(dc, "drop", "feat/a", "--no-rebuild") == 0
    out = capsys.readouterr().out
    # Both siblings: neither counts as the other's shared base.
    assert "WARNING: feat/c carries feat/a's unmerged commits" in out
    assert "WARNING: feat/d carries feat/a's unmerged commits" in out


# ── status ─────────────────────────────────────────────────────────────────


def test_status_reports_live_excluded_moved_and_stale(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"a.txt": "a1\nA-SIDE\na3\n"})
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.pr(1, "feat/a", updatedAt="2026-01-01T00:00:00Z")
    w.pr(2, "feat/b", updatedAt="2099-01-01T00:00:00Z")
    w.write_manifest([w.entry("feat/a", pr=1), w.entry("feat/b", pr=2)])
    assert w.run(dc, "rebuild") == 0
    w.advance_main({"a.txt": "a1\nMAIN-SIDE\na3\n"})
    w.git(w.root, "fetch", "-q", "origin")
    w.candidate("feat/b", {"y.txt": "y2\n"}, msg="rework")
    capsys.readouterr()
    assert w.run(dc, "status") == 0
    out = capsys.readouterr().out
    assert "ready: yes" in out
    block_a = out.split("feat/a", 1)[1].split("feat/b", 1)[0]
    block_b = out.split("feat/b", 1)[1]
    assert "live at" in block_a and "EXCLUDED" in block_a and "stale" in block_a
    assert "live at" in block_b and "moved to" in block_b and "stale" not in block_b


def test_status_says_unknown_rather_than_guessing(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.pr(1, "feat/a")
    w.write_manifest([w.entry("feat/a", pr=1)])
    w.gh_fail.add(1)
    assert w.run(dc, "status") == 0
    assert "UNKNOWN (cannot read PR #1" in capsys.readouterr().out


def test_status_names_a_candidate_whose_change_is_already_on_main(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    w.advance_main({"x.txt": "x\n"}, "the same change, squash-merged")
    w.git(w.root, "fetch", "-q", "origin")
    assert w.run(dc, "status") == 0
    assert "adds nothing beyond origin/main" in capsys.readouterr().out


# ── adopt ──────────────────────────────────────────────────────────────────


def _work_tree_id(w: World) -> str:
    """The tree of HEAD plus every tracked working-tree edit, via a scratch index."""
    idx = w.tmp / "probe.index"
    env = dict(w.env, GIT_INDEX_FILE=str(idx))
    subprocess.run(["git", "-C", str(w.root), "read-tree", "HEAD"], env=env, check=True)
    subprocess.run(["git", "-C", str(w.root), "add", "-u"], env=env, check=True)
    subprocess.run(["git", "-C", str(w.root), "add", "hand.txt"], env=env, check=True)
    return subprocess.run(
        ["git", "-C", str(w.root), "write-tree"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_adopt_sweeps_refs_of_a_killed_adopt_and_leaves_a_running_ones(dc, dc_ready, capsys):
    """Each adopt fetches into its own refs/deploy-candidates/adopt/<pid>/. A run
    killed mid-way leaves its refs behind; the next adopt removes them, and never
    the refs of an adopt that is still running."""
    w = dc_ready
    dead = subprocess.Popen(["true"])
    dead.wait()  # reaped: its pid names no process now
    alive = subprocess.Popen(["sleep", "60"])
    try:
        main = w.rev("refs/remotes/origin/main")
        for pid in (dead.pid, alive.pid):
            w.git(w.root, "update-ref", f"refs/deploy-candidates/adopt/{pid}/main", main)
        (w.root / "b.txt").write_text("edited\n")
        assert w.run(dc, "adopt", "adopt/x", "--owner", "s") == 0, capsys.readouterr()
        left = w.git(w.root, "for-each-ref", "--format=%(refname)", "refs/deploy-candidates/")
        assert left.stdout.split() == [f"refs/deploy-candidates/adopt/{alive.pid}/main"]
    finally:
        alive.kill()
        alive.wait()


def test_adopt_snapshots_the_dirty_edits_and_classifies_each_file(dc, dc_ready, capsys):
    w = dc_ready
    w.advance_main({"a.txt": "a1\nmerged\na3\n"})
    w.git(w.root, "fetch", "-q", "origin")
    pr_head = w.candidate("feat/p", {"b.txt": "from the PR\n"})
    w.pr(9, "feat/p", pr_head)
    (w.root / "a.txt").write_text("a1\nmerged\na3\n")  # equals origin/main
    (w.root / "b.txt").write_text("from the PR\n")  # equals PR #9's head
    (w.root / "hand.txt").write_text("by hand\n")
    w.git(w.root, "add", "-N", "hand.txt")  # intent-to-add: tracked now
    dirty_tree = _work_tree_id(w)
    origin_main = w.rev("refs/remotes/origin/main")
    assert w.run(dc, "adopt", "adopt/live-edits", "--owner", "sess-z") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    snap = w.rev("refs/heads/adopt/live-edits")
    assert w.rev(f"{snap}^{{tree}}") == dirty_tree
    assert w.rev(f"{snap}^") == w.rev("HEAD")
    assert re.search(r"a\.txt\s+equals origin/main", out), out
    assert re.search(r"b\.txt\s+equals PR #9", out), out
    assert re.search(r"hand\.txt\s+neither", out), out
    assert (w.root / "b.txt").read_text() == "from the PR\n"  # the checkout is untouched
    # Its fetches never move origin/main's ref, and its private refs are gone.
    assert w.rev("refs/remotes/origin/main") == origin_main
    assert w.git(w.root, "for-each-ref", "refs/deploy-candidates/").stdout == ""
    capsys.readouterr()
    assert w.run(dc, "adopt", "adopt/live-edits", "--owner", "sess-z") == 1
    assert "exists" in capsys.readouterr().err


def test_adopt_reports_the_snapshot_not_the_working_tree(dc, dc_ready, capsys, monkeypatch):
    """A file edited again while the report runs is reported as snapshotted."""
    w = dc_ready
    (w.root / "a.txt").write_text("edited\n")
    real = dc.Engine._adopt_report

    def edit_then_report(self, paths, snapshot):
        (w.root / "a.txt").write_text("a1\na2\na3\n")  # back to origin/main's content
        return real(self, paths, snapshot)

    monkeypatch.setattr(dc.Engine, "_adopt_report", edit_then_report)
    assert w.run(dc, "adopt", "adopt/x", "--owner", "s") == 0
    assert re.search(r"a\.txt\s+neither", capsys.readouterr().out)


def test_adopt_on_live_carries_only_the_edits_onto_the_base(dc, dc_ready, capsys):
    """On `live` the snapshot's branch sits on the base `live` is on: no rebuild
    merge (admission would refuse it) and no other candidate's code."""
    w = dc_ready
    _two_live(w, dc)
    live_base = w.rev("refs/remotes/origin/main")
    (w.root / "b.txt").write_text("edited on live\n")
    assert w.run(dc, "adopt", "adopt/on-live", "--owner", "s") == 0, capsys.readouterr()
    head = w.rev("refs/heads/adopt/on-live")
    assert w.rev(f"{head}^") == live_base
    files = set(w.git(w.root, "ls-tree", "-r", "--name-only", head).stdout.split())
    assert "x.txt" not in files and "y.txt" not in files
    assert w.git(w.root, "show", f"{head}:b.txt").stdout == "edited on live\n"
    capsys.readouterr()
    assert w.add(dc, "adopt/on-live") == 0, capsys.readouterr()


def test_adopt_on_live_refuses_edits_it_cannot_separate_from_a_candidate(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"b.txt": "b from a\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    (w.root / "b.txt").write_text("b from a, edited\n")
    assert w.run(dc, "adopt", "adopt/x", "--owner", "s") == 1
    assert "cannot be separated" in capsys.readouterr().err
    assert (
        w.git(w.root, "rev-parse", "--verify", "-q", "refs/heads/adopt/x", check=False).returncode
        != 0
    )


def test_adopt_reads_paths_literally_never_as_patterns(dc, dc_ready, capsys):
    w = dc_ready
    w.commit(w.root, {"w*": "one\n"}, "a file with a glob character in its name")
    w.serving_sha = w.rev("HEAD")
    (w.root / "w*").write_text("two\n")
    (w.root / "wz.txt").write_text("untracked\n")
    assert w.run(dc, "adopt", "adopt/glob", "--owner", "s") == 0, capsys.readouterr()
    names = w.git(w.root, "ls-tree", "--name-only", "refs/heads/adopt/glob").stdout.split("\n")
    assert "w*" in names and "wz.txt" not in names


@pytest.mark.parametrize(
    ("argv", "why"), [((), "nothing to adopt"), (("--owner", " "), "--owner is empty")]
)
def test_adopt_refuses(dc, dc_ready, capsys, argv, why):
    w = dc_ready
    if argv:
        (w.root / "a.txt").write_text("edit\n")
    assert w.run(dc, "adopt", "adopt/x", *(argv or ("--owner", "s"))) == 1
    assert why in capsys.readouterr().err


# ── admission ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ({"src/genesis/db/migrations/20261001000000_x.py": "x\n"}, "migration"),
        ({"src/genesis/db/data_migrations/d20261001000000_x.py": "x\n"}, "migration"),
        ({"src/genesis/db/schema/_tables.py": "x\n"}, "schema"),
        ({"src/genesis/db/schema/_migrations.py": "x\n"}, "schema"),
        ({"src/genesis/guardian/thing.py": "x\n"}, "guardian"),
        ({"config/genesis-guardian.service": "x\n"}, "guardian"),
        ({"scripts/guardian-gateway.sh": "x\n"}, "guardian"),
        ({"scripts/lib/cc_version.sh": "x\n"}, "Claude Code pin"),
        ({"scripts/cc_align_host.sh": "x\n"}, "reaches the host"),
        ({"scripts/systemd/genesis-cc-align.timer.template": "x\n"}, "reaches the host"),
        ({"scripts/cc_tmp_align_host.sh": "x\n"}, "reaches the host"),
        ({"scripts/systemd/genesis-cc-tmp-align.service.template": "x\n"}, "reaches the host"),
        ({"scripts/hooks/pre-push": "#!/bin/sh\n"}, "hook"),
        ({".claude/hooks/genesis-hook": "#!/bin/sh\n"}, "hook"),
        ({"scripts/update.sh": "x\n"}, "keeps the wipers"),
        ({"scripts/deploy_code_only.sh": "x\n"}, "keeps the wipers"),
        ({"scripts/lib/guardian_pause.sh": "x\n"}, "keeps the wipers"),
        ({"scripts/lib/deploy_checkout.sh": "x\n"}, "keeps the wipers"),
        ({"src/genesis/dashboard/routes/updates.py": "x\n"}, "keeps the wipers"),
        ({"scripts/deploy_candidates_gate.py": "x\n"}, "keeps the wipers"),
        ({"scripts/lib/deploy_live.sh": "x\n"}, "keeps the wipers"),
    ],
)
def test_admission_refuses_what_must_not_go_live(dc, dc_ready, capsys, files, why):
    w = dc_ready
    w.candidate("feat/x", files)
    assert w.add(dc, "feat/x") == 1
    assert why in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_admission_refuses_a_change_to_a_merged_migration(dc, dc_ready, capsys, monkeypatch):
    """A migration main already carries but this install has not applied yet runs
    at the next boot in whatever form `live` holds, so changing one is refused
    exactly like adding one."""
    w = dc_ready
    mig = "src/genesis/db/migrations/20261001000000_x.py"
    base = w.advance_main({mig: "x = 1\n"})
    monkeypatch.setattr(dc.gate, "REQUIRED_MERGED", (("scratch base", base),))
    w.serving_sha = base
    w.candidate("feat/x", {mig: "x = 2\n"})
    assert w.add(dc, "feat/x") == 1
    assert "changes a migration" in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_admission_allows_shared_runtime_and_the_accepted_hook_surface_residual(
    dc, dc_ready, capsys
):
    """update.sh redeploys the host guardian when src/genesis/db, util,
    observability, env.py or pyproject.toml change; those are ordinary runtime
    code here. .claude/settings.json and the scripts/-root hooks are admitted by
    the owner's ruling (only the two hook directories are refused in v1): pinned
    here so changing it is a decision, not a drift."""
    w = dc_ready
    w.candidate(
        "feat/x",
        {
            "src/genesis/db/crud/thing.py": "x\n",
            "src/genesis/util/u.py": "u\n",
            "pyproject.toml": "[x]\n",
            ".claude/settings.json": "{}\n",
            "scripts/review_enforcement_commit.py": "# x\n",
        },
    )
    assert w.add(dc, "feat/x") == 0, capsys.readouterr()


def test_admission_refuses_a_branch_cut_from_live(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    assert w.run(dc, "rebuild") == 0
    w.candidate("feat/bad", {"q.txt": "q\n"}, base="live")
    capsys.readouterr()
    assert w.add(dc, "feat/bad") == 1
    assert "Deploy-rebuild" in capsys.readouterr().err


def test_the_guardian_rule_accounts_for_every_path_update_sh_redeploys(dc):
    """Every entry of update.sh's GUARDIAN_PATHS is either refused as the
    guardian's own, or named as shared runtime code."""
    text = (SCRIPTS / "update.sh").read_text()
    lists = re.findall(r'^\s*GUARDIAN_PATHS="([^"]+)"', text, re.MULTILINE)
    assert len(lists) == 1, "update.sh must carry exactly one GUARDIAN_PATHS list"
    g = dc.gate
    for path in lists[0].split():
        own = path in g.GUARDIAN_OWN_FILES or any(
            path.startswith(p) or (path + "/").startswith(p) for p in g.GUARDIAN_OWN_PREFIXES
        )
        assert own != (path in g.GUARDIAN_SHARED_RUNTIME), path


# Every script a systemd unit runs from this checkout, classified: refused
# (it drives the host) or admitted, with the reason. A new unit fails here
# until someone decides which.
_CONTAINER_LOCAL_UNITS = {
    "scripts/backup.sh": "runs in the container with the access the server already gives candidate code",
    "scripts/cc_settings_align.sh": "container-side Claude Code settings only",
    "scripts/code_intel_freeze.sh": "container-side index locks only",
    "scripts/code_intel_runner.sh": "container-side code index only",
    "scripts/disk_hygiene.sh": "container-side file retention only",
    "scripts/graph_project_runner.sh": "container-side graph projection only",
    "scripts/tmp_watchgod.sh": "container-side disk guard only",
}


def test_every_systemd_unit_target_is_classified(dc):
    targets = set()
    for unit in (SCRIPTS / "systemd").glob("*.template"):
        for line in unit.read_text().splitlines():
            if line.startswith("ExecStart="):
                targets.update(re.findall(r"__REPO_DIR__/(\S+)", line))
    assert targets, "no ExecStart target found: the enumeration is broken"
    for target in sorted(targets):
        refused = dc.gate.path_refusal(target) is not None
        assert refused != (target in _CONTAINER_LOCAL_UNITS), target


def test_every_lib_the_refusing_scripts_source_before_their_check_is_refused(dc):
    """update.sh and deploy_code_only.sh refuse to run on `live` from a branch
    check; anything they source BEFORE it runs first, so a candidate editing it
    could switch the refusal off."""
    for script in ("update.sh", "deploy_code_only.sh"):
        text = (SCRIPTS / script).read_text()
        check = text.index('genesis_deploy_branch_ok "$GENESIS_ROOT"')
        sourced = re.findall(
            r'^\s*(?:\.|source) "\$[A-Z_]+/(lib/[^"]+)"', text[:check], re.MULTILINE
        )
        assert sourced, script
        for lib in sourced:
            assert dc.gate.path_refusal(f"scripts/{lib}"), f"{script} sources scripts/{lib} first"


def test_the_engine_is_inert_in_this_repository_until_pr_c_lands(dc):
    """PR C creates the readiness marker file; until then no install is ready.
    When PR C lands it deletes this test along with adding the file."""
    res = subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "-e", f"HEAD:{dc.gate.PR_C_MARKER}"],
        capture_output=True,
    )
    assert res.returncode != 0


def test_the_real_required_commit_is_not_taken_on_trust(dc, dc_world, capsys):
    """Without the scratch stand-in, the real required commit does not exist in
    the scratch repository: readiness says so rather than passing."""
    w = dc_world
    w.serving_sha = w.rev("refs/remotes/origin/main")
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.add(dc, "feat/x") == 1
    err = capsys.readouterr().err
    assert "#2673" in err and "not in this repository" in err
    assert not w.manifest_path.exists()


# ── parsing other files ────────────────────────────────────────────────────


def test_the_hook_list_is_read_from_both_sync_hooks_arrays(dc):
    text = (SCRIPTS / "hooks" / "sync-hooks.sh").read_text()
    assert dc.gate.sync_hook_names(text) == list(HOOK_NAMES) + list(HELPER_NAMES)


@pytest.mark.parametrize(
    "extra",
    [
        'HELPERS_TO_SYNC+=("another.py")\n',
        'HOOKS_TO_SYNC=(\n    "x"\n)\n',
        "HELPERS_TO_SYNC=(\n    bare\n)\n",
    ],
)
def test_a_sync_hooks_list_read_short_refuses(dc, extra):
    text = (SCRIPTS / "hooks" / "sync-hooks.sh").read_text() + extra
    with pytest.raises(dc.core.Refusal):
        dc.gate.sync_hook_names(text)


@pytest.mark.parametrize(
    ("stdout", "want"),
    [
        ("serving: " + "a" * 40 + "\nhead: x\n", "a" * 40),
        ("serving: unknown (genesis-server is not active)\n", None),
        ("serving: \n", None),
        ("head: x\n", None),
        ("serving: " + "a" * 64 + "\n", None),  # a SHA-256 repository: unknown, never a guess
    ],
)
def test_the_serving_line_is_parsed_or_unknown(dc, stdout, want):
    got, why = dc.core.parse_serving(stdout, 0)
    assert got == want and (want is not None or why)


def test_the_engine_sources_the_ephemeral_regex_never_defines_it():
    for name in ENGINE_FILES:
        text = (SCRIPTS / name).read_text()
        assert "EPHEMERAL_DIRTY_RE=" not in text.replace('"$EPHEMERAL_DIRTY_RE"', ""), name
    assert re.search(r'^\. "\$_SELF_DIR/lib/deploy_marker\.sh"$', ENTRY.read_text(), re.MULTILINE)


def test_nothing_in_the_environment_can_point_the_engine_at_another_checkout():
    for name in ENGINE_FILES:
        assert "GENESIS_DEPLOY_CANDIDATES_ROOT" not in (SCRIPTS / name).read_text(), name


# ── the shell entry ────────────────────────────────────────────────────────


def test_drop_list_and_status_work_from_a_plain_shell(dc, dc_ready):
    w = dc_ready
    hb = _two_live(w, dc)
    w.install_engine()
    res = w.plain_shell("list")
    assert res.returncode == 0 and "feat/a" in res.stdout, res.stdout + res.stderr
    res = w.plain_shell("status")
    assert res.returncode == 0 and "feat/b" in res.stdout, res.stdout + res.stderr
    res = w.plain_shell("drop", "feat/a")
    assert res.returncode == 0, res.stdout + res.stderr
    assert w.live_merges() == [("feat/b", hb)]
    assert not (w.home / ".genesis" / "update_in_progress.pid").exists()


def test_a_live_candidate_cannot_shadow_the_engines_imports(dc, dc_ready, tmp_path):
    """The engine runs from the checkout it judges. A live candidate that adds
    scripts/argparse.py (any standard-library name) must not run inside the
    engine: `drop`, the repair path, still removes it. Checked through the
    entry (python3 -P) and through a bare `python3 scripts/deploy_candidates.py`,
    which puts scripts/ first on sys.path unless the engine takes it off."""
    w = dc_ready
    sentinel = tmp_path / "candidate-code-ran"
    shadow = f"open({str(sentinel)!r}, 'w').write('ran')\nraise SystemExit(0)\n"
    # argparse/json exist in the standard library; msvcrt and nt do not exist on
    # Linux, but the standard library LOOKS them up on every run (subprocess and
    # ntpath), so any sys.path entry holding them answers the lookup.
    w.candidate(
        "feat/evil",
        {
            "scripts/argparse.py": shadow,
            "scripts/json.py": shadow,
            "scripts/msvcrt.py": shadow,
            "scripts/nt.py": shadow,
        },
    )
    w.write_manifest([w.entry("feat/evil")])
    assert w.run(dc, "rebuild") == 0
    assert (w.root / "scripts" / "argparse.py").exists()  # it is live
    w.install_engine()
    direct = subprocess.run(
        [
            "env",
            "-i",
            f"HOME={w.home}",
            "GIT_CONFIG_NOSYSTEM=1",
            "/usr/bin/python3",
            str(w.root / "scripts" / "deploy_candidates.py"),
            "list",
        ],
        capture_output=True,
        text=True,
    )
    assert direct.returncode == 0 and "feat/evil" in direct.stdout, direct.stdout + direct.stderr
    res = w.plain_shell("drop", "feat/evil")
    assert res.returncode == 0, res.stdout + res.stderr
    assert not sentinel.exists()
    assert w.live_merges() == []
    assert not (w.root / "scripts" / "argparse.py").exists()


def _shared_hold(w: World):
    lock_path = w.home / ".genesis" / "locks" / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_SH)  # a validation's shared hold
    return fd


@pytest.mark.parametrize(
    "argv",
    [("drop", "feat/a"), ("drop", "--no-rebuild", "feat/a"), ("drop", "feat/a", "--no-rebuild")],
)
def test_every_drop_form_queues_on_the_update_lock(dc, dc_ready, argv):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    before = w.manifest_path.read_text()
    fd = _shared_hold(w)
    try:
        res = w.plain_shell(*argv, "--wait", "1")
        assert res.returncode == 200, res.stdout + res.stderr
        assert w.manifest_path.read_text() == before
    finally:
        os.close(fd)
    res = w.plain_shell(*argv, "--wait", "1")
    assert res.returncode == 0, res.stdout + res.stderr


def test_add_queues_on_the_update_lock_and_takes_no_deploy_marker(dc, dc_ready):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    w.install_engine()
    fd = _shared_hold(w)
    try:
        res = w.plain_shell("add", "feat/x", "--owner", "s", "--wait", "1")
        assert res.returncode == 200, res.stdout + res.stderr
    finally:
        os.close(fd)
    text = ENTRY.read_text()
    assert re.search(r"^\s+add\) needs_lock=1 ;;$", text, re.MULTILINE)


def test_the_entry_refuses_a_live_foreign_deploy_marker(dc, dc_ready):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (w.home / ".genesis" / "update_in_progress.pid").write_text(f"{holder.pid}\n")
        res = w.plain_shell("rebuild", "--wait", "1")
        assert res.returncode == 1 and "deploy marker" in res.stderr, res.stdout + res.stderr
    finally:
        holder.kill()
        holder.wait()
