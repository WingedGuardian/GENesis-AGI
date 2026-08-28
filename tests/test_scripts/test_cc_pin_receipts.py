"""CC pin-receipt CI guard (scripts/check_cc_pin_receipts.py).

A PR that moves the Claude Code pin FORWARD must carry both gate receipts in
its body; a pin that moves BACKWARD is exempt (the downgrade path is the
project's incident-recovery route). Everything that cannot be determined
SKIPS rather than blocking.

Each test builds a real throwaway git repo so the base-vs-head pin comparison
runs through the same ``git show`` path CI uses.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_cc_pin_receipts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_cc_pin_receipts", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


BOTH_RECEIPTS = """\
Some ordinary PR prose.

CC-Gate-Changelog: read (2.1.218, 2.1.246] in full from CHANGELOG.md, 2026-08-27
CC-Gate-Soak: 2.1.246 on container 2026-08-25..2026-08-27, sweep clean, signed off
"""


def _pin_file(version: str) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "# Honors an inherited CC_VERSION.\n"
        f'CC_VERSION="${{CC_VERSION:-{version}}}"\n'
        'NODE_MAJOR="${NODE_MAJOR:-22}"\n'
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )


@pytest.fixture
def repo(tmp_path: Path):
    """A git repo whose HEAD~ pins 2.1.218; caller sets the HEAD pin."""
    r = tmp_path / "repo"
    (r / "scripts" / "lib").mkdir(parents=True)
    _git(r.parent, "init", "-q", "-b", "main", str(r))
    (r / "scripts" / "lib" / "cc_version.sh").write_text(_pin_file("2.1.218"))
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD").stdout.strip()

    def set_head_pin(version: str, *, extra: str = "") -> str:
        p = r / "scripts" / "lib" / "cc_version.sh"
        p.write_text(_pin_file(version) + extra)
        _git(r, "add", "-A")
        _git(r, "commit", "-qm", "head")
        return base

    return {"path": r, "base": base, "set_head_pin": set_head_pin}


# ── the pin didn't move ───────────────────────────────────────────────────


def test_pin_unchanged_passes(repo):
    """The #1497 shape: cc_version.sh heavily edited, pin untouched."""
    repo["set_head_pin"](
        "2.1.218",
        extra="\ncc_ensure_updater_suppressed() { :; }\n" * 20,
    )

    msg = mod.check(base_sha=repo["base"], body="", repo_root=repo["path"])

    assert "unchanged" in msg


# ── forward moves ─────────────────────────────────────────────────────────


def test_forward_with_both_receipts_passes(repo):
    repo["set_head_pin"]("2.1.246")

    msg = mod.check(base_sha=repo["base"], body=BOTH_RECEIPTS, repo_root=repo["path"])

    assert "moves forward" in msg
    assert "2.1.218" in msg and "2.1.246" in msg


@pytest.mark.parametrize(
    "drop,expected",
    [
        ("CC-Gate-Changelog", "CC-Gate-Changelog"),
        ("CC-Gate-Soak", "CC-Gate-Soak"),
    ],
)
def test_forward_missing_one_receipt_blocks(repo, drop, expected):
    repo["set_head_pin"]("2.1.246")
    body = "\n".join(line for line in BOTH_RECEIPTS.splitlines() if not line.startswith(drop))

    with pytest.raises(mod.MissingReceipts) as exc:
        mod.check(base_sha=repo["base"], body=body, repo_root=repo["path"])

    assert expected in str(exc.value)


def test_forward_missing_both_names_both(repo):
    repo["set_head_pin"]("2.1.246")

    with pytest.raises(mod.MissingReceipts) as exc:
        mod.check(base_sha=repo["base"], body="just prose", repo_root=repo["path"])

    text = str(exc.value)
    assert "CC-Gate-Changelog" in text
    assert "CC-Gate-Soak" in text
    assert "2 required gate receipt" in text


def test_pasted_template_placeholders_do_not_satisfy_the_gate(repo):
    """The doc ships a copy-pasteable example; pasting it verbatim says nothing.

    A presence-only check would pass `CC-Gate-Soak: <candidate> on container
    <start>..<end>` — the exact text from the doc — which is the single most
    likely way to satisfy this gate without having run it.
    """
    repo["set_head_pin"]("2.1.246")
    body = (
        "CC-Gate-Changelog: read (<X>, <Y>] in full from <source>, <date>\n"
        "CC-Gate-Soak: <candidate> on container <start>..<end>, sign-off recorded\n"
    )

    with pytest.raises(mod.MissingReceipts) as exc:
        mod.check(base_sha=repo["base"], body=body, repo_root=repo["path"])

    text = str(exc.value)
    assert "CC-Gate-Changelog" in text
    assert "CC-Gate-Soak" in text


def test_real_values_containing_angle_brackets_still_pass(repo):
    """Guard against over-rejection: a filled receipt must not trip the check.

    Only `<word>`-shaped placeholders count; a range like `(2.1.218, 2.1.246]`
    and a comparison in prose must remain acceptable.
    """
    repo["set_head_pin"]("2.1.246")

    msg = mod.check(base_sha=repo["base"], body=BOTH_RECEIPTS, repo_root=repo["path"])

    assert "moves forward" in msg


def test_bare_marker_without_a_value_is_not_a_receipt(repo):
    """`CC-Gate-Soak:` with nothing after it must not satisfy the gate."""
    repo["set_head_pin"]("2.1.246")
    body = "CC-Gate-Changelog: read (2.1.218, 2.1.246] in full, 2026-08-27\nCC-Gate-Soak:\n"

    with pytest.raises(mod.MissingReceipts) as exc:
        mod.check(base_sha=repo["base"], body=body, repo_root=repo["path"])

    assert "CC-Gate-Soak" in str(exc.value)


# ── downgrades are exempt ─────────────────────────────────────────────────


def test_downgrade_is_exempt_without_receipts(repo):
    """Incident recovery must not need a soak receipt — or any ceremony."""
    repo["set_head_pin"]("2.1.87")

    msg = mod.check(base_sha=repo["base"], body="", repo_root=repo["path"])

    assert "BACKWARD" in msg
    assert "exempt" in msg


def test_downgrade_comparison_is_numeric_not_lexical(repo):
    """2.1.87 < 2.1.218 numerically, but '2.1.87' > '2.1.218' as strings.

    A lexical compare would read this rollback as a forward move and demand
    receipts during an incident — the exact failure the exemption exists to
    prevent.
    """
    assert mod.version_tuple("2.1.87") < mod.version_tuple("2.1.218")
    assert "2.1.87" > "2.1.218"  # the trap, spelled out

    repo["set_head_pin"]("2.1.87")
    msg = mod.check(base_sha=repo["base"], body="", repo_root=repo["path"])
    assert "BACKWARD" in msg


# ── everything indeterminate SKIPS ────────────────────────────────────────


def test_no_base_sha_skips(repo):
    with pytest.raises(mod.Skip):
        mod.check(base_sha="", body=BOTH_RECEIPTS, repo_root=repo["path"])


def test_unreachable_base_ref_skips(repo):
    """A shallow clone can't see the base — say so, don't block every PR."""
    repo["set_head_pin"]("2.1.246")

    with pytest.raises(mod.Skip) as exc:
        mod.check(base_sha="0" * 40, body=BOTH_RECEIPTS, repo_root=repo["path"])

    assert "cannot read" in str(exc.value)


def test_forward_with_an_empty_body_BLOCKS(repo):
    """An empty PR body on a forward bump is a violation, not an unknown.

    This test previously asserted the OPPOSITE and thereby encoded a fail-open
    AS THE SPEC: a bodyless PR is one of the most common inputs there is, and the
    absence of both receipts is fully determined by it. Nothing is indeterminate,
    so there is nothing to be graceful about. `Skip` stays reserved for values
    that are genuinely unavailable (no base SHA, unfetched base ref).
    """
    repo["set_head_pin"]("2.1.246")

    with pytest.raises(mod.MissingReceipts) as exc:
        mod.check(base_sha=repo["base"], body="   ", repo_root=repo["path"])

    text = str(exc.value)
    assert "CC-Gate-Changelog" in text
    assert "CC-Gate-Soak" in text


def test_a_bare_trailer_cannot_borrow_the_next_lines_value(repo):
    """`\\s*` matches NEWLINES — so an empty trailer swallowed the line below it.

    With plain `\\s*`, "CC-Gate-Changelog:\nCC-Gate-Soak: 2.1.246 ..." satisfied
    BOTH markers from one real receipt: the empty changelog trailer matched
    across the newline and took the soak line as its value.
    """
    repo["set_head_pin"]("2.1.246")
    body = (
        "CC-Gate-Changelog:\n"
        "CC-Gate-Soak: 2.1.246 on container 08-25..08-27, sweep clean, sign-off recorded\n"
    )

    with pytest.raises(mod.MissingReceipts) as exc:
        mod.check(base_sha=repo["base"], body=body, repo_root=repo["path"])

    assert "CC-Gate-Changelog" in str(exc.value)


def test_two_bare_trailers_followed_by_prose_satisfy_nothing(repo):
    """The same newline bug, in the shape that needs no real receipt at all."""
    repo["set_head_pin"]("2.1.246")
    body = "CC-Gate-Changelog:\nCC-Gate-Soak:\nsome ordinary prose about the change\n"

    with pytest.raises(mod.MissingReceipts) as exc:
        mod.check(base_sha=repo["base"], body=body, repo_root=repo["path"])

    text = str(exc.value)
    assert "CC-Gate-Changelog" in text
    assert "CC-Gate-Soak" in text


def test_unparseable_head_pin_skips(repo):
    repo["set_head_pin"]("2.1.246")
    (repo["path"] / "scripts" / "lib" / "cc_version.sh").write_text("nothing here\n")

    with pytest.raises(mod.Skip):
        mod.check(base_sha=repo["base"], body=BOTH_RECEIPTS, repo_root=repo["path"])


# ── parsing units ─────────────────────────────────────────────────────────


def test_parse_pin_matches_the_real_repo_file():
    """Smoke: the regex must parse the live cc_version.sh, not just fixtures."""
    text = (_REPO_ROOT / "scripts" / "lib" / "cc_version.sh").read_text()

    assert mod.parse_pin(text) is not None


def test_parse_pin_returns_none_when_absent():
    assert mod.parse_pin('NODE_MAJOR="${NODE_MAJOR:-22}"\n') is None


def test_exit_code_missing_receipts_is_one(repo, tmp_path):
    """End-to-end through main(): a real non-zero exit is what CI reads."""
    repo["set_head_pin"]("2.1.246")
    body_file = tmp_path / "body.md"
    body_file.write_text("no receipts here")

    rc = mod.main(
        [
            "--base-sha",
            repo["base"],
            "--body-file",
            str(body_file),
            "--repo-root",
            str(repo["path"]),
        ]
    )

    assert rc == 1


def test_unexpected_error_in_the_guard_does_not_wall_off_the_repo(repo, tmp_path, monkeypatch):
    """A bug in this guard must degrade to SKIP, never to a red CI on every PR.

    There are no required status checks on this repo, but the local merge gate
    blocks on a red CI rollup — so an unhandled exception here would stop every
    unrelated PR from merging over a check that only guards a pin bump.
    """
    body_file = tmp_path / "body.md"
    body_file.write_text(BOTH_RECEIPTS)

    def _boom(**_kwargs):
        raise ValueError("simulated bug inside the guard")

    monkeypatch.setattr(mod, "check", _boom)

    rc = mod.main(
        [
            "--base-sha",
            repo["base"],
            "--body-file",
            str(body_file),
            "--repo-root",
            str(repo["path"]),
        ]
    )

    assert rc == 0


def test_exit_code_downgrade_is_zero(repo, tmp_path):
    repo["set_head_pin"]("2.1.87")
    body_file = tmp_path / "body.md"
    body_file.write_text("")

    rc = mod.main(
        [
            "--base-sha",
            repo["base"],
            "--body-file",
            str(body_file),
            "--repo-root",
            str(repo["path"]),
        ]
    )

    assert rc == 0


# ── the workflow trigger is part of the gate ──────────────────────────────


def test_workflow_reruns_when_the_pr_body_is_edited():
    """The gate reads the PR BODY, which is mutable after a run completes.

    GitHub's default `pull_request` activity types are opened / synchronize /
    reopened. With only those, an author could add the receipts after a red run
    and never get re-checked — or DELETE them after a green one and merge a
    status describing a body that no longer exists. `edited` is what makes the
    reported status describe the body that actually merges.

    It also must NOT live in ci.yml: activity types are per-WORKFLOW, so `edited`
    there would re-run the whole suite (the ~23-minute test job included) on
    every description tweak.
    """
    import yaml

    wf_dir = _REPO_ROOT / ".github" / "workflows"
    wf = yaml.safe_load((wf_dir / "cc-pin-receipts.yml").read_text())
    # PyYAML parses a bare `on:` key as the boolean True.
    trigger = wf.get("on", wf.get(True))
    types = trigger["pull_request"]["types"]

    assert "edited" in types, (
        "without `edited` the receipts can be removed after a green run"
    )
    for required in ("opened", "synchronize", "reopened"):
        assert required in types, (
            f"naming `types` REPLACES the defaults, so {required} must be listed"
        )

    ci = yaml.safe_load((wf_dir / "ci.yml").read_text())
    assert "cc-pin-receipts" not in ci["jobs"], (
        "the receipt job must stay OUT of ci.yml — sharing that workflow's "
        "trigger would either lose `edited` or re-run the full suite on every "
        "body edit"
    )


def test_workflow_compares_against_the_merge_refs_first_parent():
    """base.sha lags main; HEAD^1 of the merge ref does not.

    With the stale value, an unrelated long-lived PR re-running after a pin bump
    landed on main compares the merge result's NEW pin against the OLD one and
    demands receipts from a PR that never touched the pin. ci.yml's leak-scan
    anchors on HEAD^1 for exactly this reason.
    """
    import yaml

    wf = yaml.safe_load(
        (_REPO_ROOT / ".github" / "workflows" / "cc-pin-receipts.yml").read_text()
    )
    steps = wf["jobs"]["cc-pin-receipts"]["steps"]
    # Strip shell comments: the run block explains WHY base.sha is wrong, and
    # a naive substring check would read that explanation as the defect itself.
    run_blocks = "\n".join(
        line
        for s in steps
        if "run" in s
        for line in s["run"].splitlines()
        if not line.lstrip().startswith("#")
    )
    env_blocks = "\n".join(
        f"{k}={v}" for s in steps for k, v in (s.get("env") or {}).items()
    )

    assert "HEAD^1" in run_blocks
    # Assert on the EXECUTED step, not the file text — the surrounding comment
    # names base.sha to explain why it is wrong, and a whole-file substring
    # check would flag that explanation as the defect it warns about.
    assert "pull_request.base.sha" not in run_blocks + env_blocks, (
        "base.sha lags main — use the merge ref's first parent"
    )
