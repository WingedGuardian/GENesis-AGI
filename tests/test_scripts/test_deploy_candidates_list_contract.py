"""The engine's `list --json` output is a contract: deploy_health derives every
`live` finding from it (it may not read the deploy manifest itself, by the reader
policy in test_deploy_candidates_manifest.py). Each manifest and `live` state must
come out as its own value, and nothing may read as an empty, healthy answer that
the engine could not establish."""

from __future__ import annotations

import json
import os

import pytest

from tests.test_scripts._deploy_candidates_world import World


def _observe(w: World, dc, capsys) -> dict:
    capsys.readouterr()
    assert w.run(dc, "list", "--json") == 0
    return json.loads(capsys.readouterr().out)


def _two_live(w: World, dc) -> None:
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0


def _branches(rows) -> list[str]:
    return [r["branch"] for r in rows]


def test_no_manifest_and_no_live(dc, dc_ready, capsys):
    got = _observe(dc_ready, dc, capsys)
    assert got["manifest"] == {"state": "absent", "reason": None}
    assert got["listed"] == []
    assert got["live"] == {"tip": None, "holds": [], "reason": None}
    assert got["base"] == dc_ready.rev("refs/remotes/origin/main")


def test_listed_and_held_after_a_rebuild(dc, dc_ready, capsys):
    w = dc_ready
    _two_live(w, dc)
    got = _observe(w, dc, capsys)
    assert got["manifest"]["state"] == "ok"
    assert _branches(got["listed"]) == ["feat/a", "feat/b"]
    assert not any(r["in_base"] for r in got["listed"])
    assert _branches(got["live"]["holds"]) == ["feat/a", "feat/b"]
    assert got["live"]["tip"] == w.rev("refs/heads/live")


def test_a_listed_candidate_with_no_live_ref_yet(dc, dc_ready, capsys):
    """The first `add` writes only the manifest: `live` does not exist until a
    rebuild, and the candidate is listed, not held (round-2 review)."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/a")])
    got = _observe(w, dc, capsys)
    assert _branches(got["listed"]) == ["feat/a"]
    assert got["live"] == {"tip": None, "holds": [], "reason": None}


def test_added_but_not_rebuilt_is_listed_not_held(dc, dc_ready, capsys):
    w = dc_ready
    _two_live(w, dc)
    w.candidate("feat/c", {"z.txt": "z\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b"), w.entry("feat/c")])
    got = _observe(w, dc, capsys)
    assert _branches(got["listed"]) == ["feat/a", "feat/b", "feat/c"]
    assert _branches(got["live"]["holds"]) == ["feat/a", "feat/b"]


def test_dropped_without_a_rebuild_is_held_not_listed(dc, dc_ready, capsys):
    w = dc_ready
    _two_live(w, dc)
    assert w.run(dc, "drop", "feat/a", "--no-rebuild") == 0
    got = _observe(w, dc, capsys)
    assert _branches(got["listed"]) == ["feat/b"]
    assert sorted(_branches(got["live"]["holds"])) == ["feat/a", "feat/b"]


def test_a_candidate_already_in_origin_main_is_marked_in_base(dc, dc_ready, capsys):
    """No rebuild merges a candidate whose pinned head origin/main contains, so
    `live` never holds it: in_base says that is expected, not unbuilt."""
    w = dc_ready
    base = w.rev("refs/remotes/origin/main")
    w.write_manifest([w.entry("feat/old", head=base)])
    got = _observe(w, dc, capsys)
    assert got["listed"] == [{"branch": "feat/old", "head": base, "in_base": True}]


@pytest.mark.parametrize("text", ["{not json", '{"version": 3}'], ids=["unparseable", "malformed"])
def test_a_broken_manifest_is_an_error_and_live_is_still_read(dc, dc_ready, capsys, text):
    """A manifest problem never hides what `live` holds, and never reads as absent."""
    w = dc_ready
    _two_live(w, dc)
    w.manifest_path.write_text(text)
    got = _observe(w, dc, capsys)
    assert got["manifest"]["state"] == "error" and got["manifest"]["reason"]
    assert got["listed"] == []
    assert _branches(got["live"]["holds"]) == ["feat/a", "feat/b"]


def test_a_manifest_of_another_repository_is_foreign_not_an_error(dc, dc_ready, capsys, tmp_path):
    """The predicate reads such a manifest as `other` (this checkout's own rules
    apply); the engine says so rather than reporting a broken manifest."""
    w = dc_ready
    other = tmp_path / "other.git"
    w.git(w.root, "init", "-q", "--bare", str(other))
    w.manifest_path.write_text(
        json.dumps({"version": 3, "repo": os.path.realpath(other), "candidates": []})
    )
    got = _observe(w, dc, capsys)
    assert got["manifest"]["state"] == "foreign"
    assert got["listed"] == []


def test_from_a_linked_worktree_stdout_is_one_json_object(dc, dc_ready, capsys):
    """`place` names the main checkout it reports; under --json that note goes to
    stderr so stdout parses (round-3 design review)."""
    w = dc_ready
    w.candidate("feat/a", {"x.txt": "x\n"})  # makes a linked worktree
    wt = w.tmp / "wt-feat-a"
    capsys.readouterr()
    rc = dc.main(["list", "--json"], env=dict(w.env), gh=w.gh, serving=w.serving, root=wt)
    assert rc == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["manifest"]["state"] == "absent"
    assert "from a linked worktree" in cap.err


def test_plain_list_is_unchanged(dc, dc_ready, capsys):
    capsys.readouterr()
    assert dc_ready.run(dc, "list") == 0
    assert capsys.readouterr().out.startswith("No deploy manifest")
