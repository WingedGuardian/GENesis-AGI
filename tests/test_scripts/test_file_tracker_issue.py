"""Tests for scripts/file_tracker_issue.py.

Every test here corresponds to a defect a cross-model reviewer found in the
PROSE version of this procedure across three rounds. The point of moving it into
a script was that these become checkable; the point of these tests is that the
class cannot come back silently.

No network, no `gh`, no live repo — the runner is injected.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "file_tracker_issue.py"
_spec = importlib.util.spec_from_file_location("file_tracker_issue", _SCRIPT)
assert _spec and _spec.loader
fti = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fti)


def _proc(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _runner(responses: dict[str, subprocess.CompletedProcess[str]], calls: list | None = None):
    """Dispatch on a distinctive substring of the argv, recording every call."""

    def run(argv):
        if calls is not None:
            calls.append(list(argv))
        joined = " ".join(argv)
        for key, resp in responses.items():
            if key in joined:
                return resp
        raise AssertionError(f"unexpected command: {joined}")

    return run


# --- round 3: the permission check resolved the FORK and reported ADMIN -------


def test_fork_resolves_to_parent_not_the_fork():
    """A fork clone must target the upstream tracker, never the operator's fork."""
    run = _runner(
        {
            "isFork": _proc(
                json.dumps(
                    {
                        "isFork": True,
                        "nameWithOwner": "downstream/Proj",
                        "parent": {"owner": {"login": "Upstream"}, "name": "Proj"},
                    }
                )
            )
        }
    )
    assert fti.resolve_tracker(run) == "Upstream/Proj"


def test_fork_without_resolvable_parent_refuses():
    run = _runner(
        {"isFork": _proc(json.dumps({"isFork": True, "nameWithOwner": "alice/x", "parent": None}))}
    )
    with pytest.raises(fti.Refused, match="parent could not be resolved"):
        fti.resolve_tracker(run)


def test_non_fork_uses_current_repo():
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            )
        }
    )
    assert fti.resolve_tracker(run) == "Org/Repo"


# --- round 2: identity was checked instead of PERMISSION ---------------------


@pytest.mark.parametrize("perm", ["WRITE", "MAINTAIN", "ADMIN"])
def test_write_permissions_accepted(perm):
    run = _runner({"viewerPermission": _proc(json.dumps({"viewerPermission": perm}))})
    assert fti.check_permission("Org/Repo", run) == perm


@pytest.mark.parametrize("perm", ["READ", "TRIAGE", None])
def test_insufficient_permission_refuses(perm):
    """GitHub silently DROPS labels without push access — so this must fail closed."""
    run = _runner({"viewerPermission": _proc(json.dumps({"viewerPermission": perm}))})
    with pytest.raises(fti.Refused, match="no write access"):
        fti.check_permission("Org/Repo", run)


def test_permission_is_checked_on_the_resolved_slug_not_the_cwd():
    """The slug must be passed EXPLICITLY — a bare `gh repo view` reads the cwd."""
    calls: list = []
    run = _runner({"viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"}))}, calls)
    fti.check_permission("Upstream/Proj", run)
    assert "Upstream/Proj" in calls[0]


# --- round 3: `--search` parsed query syntax inside a title ------------------


def test_duplicate_check_does_not_use_search():
    """A title containing `repo:`/`is:` must be compared, not interpreted."""
    calls: list = []
    run = _runner({"issue list": _proc(json.dumps([{"number": 5, "title": "unrelated"}]))}, calls)
    fti.find_duplicate("Org/Repo", "fix repo: parsing in is: handler", run)
    assert "--search" not in " ".join(calls[0])


def test_exact_title_match_is_found_and_near_miss_is_not():
    listing = json.dumps(
        [
            {"number": 11, "title": "Fix   the   PARSER  "},
            {"number": 12, "title": "Fix the parser eventually"},
        ]
    )
    run = _runner({"issue list": _proc(listing)})
    assert fti.find_duplicate("Org/Repo", "fix the parser", run) == 11
    assert fti.find_duplicate("Org/Repo", "totally different title", run) is None


def test_failed_duplicate_lookup_refuses_rather_than_assuming_none():
    """A failed lookup is not evidence of no duplicate."""
    run = _runner({"issue list": _proc(returncode=1, stderr="network down")})
    with pytest.raises(fti.Refused, match="Refusing to file"):
        fti.find_duplicate("Org/Repo", "anything", run)


# --- round 3: area:docs / area:infra do not exist and make `gh` fail ---------


def test_plausible_but_nonexistent_area_labels_are_rejected():
    """The routing rule says 'docs, infra' — so these are what an agent reaches for."""
    for bogus in ("area:docs", "area:infra", "area:tooling"):
        with pytest.raises(fti.Refused, match="not a real area label"):
            fti.validate_labels(bogus, "help wanted")


def test_bad_difficulty_label_rejected_and_good_pair_accepted():
    with pytest.raises(fti.Refused, match="not a real difficulty"):
        fti.validate_labels("area:memory", "easy")
    assert fti.validate_labels("area:memory", "help wanted") == ["area:memory", "help wanted"]


def test_label_sets_match_the_canonical_server_side_sets():
    """Drift-lock: these mirror contributor_issue.py, which enforces them fail-closed.

    A mirror that silently drifts is worse than no mirror — this asserts the WHOLE
    set both ways, not membership of the few labels we happened to think of.
    """
    src = (
        Path(__file__).resolve().parents[2] / "src/genesis/mcp/health/contributor_issue.py"
    ).read_text()
    ns: dict = {"frozenset": frozenset}
    for name in ("_AREA_LABELS", "_ENV_DIFFICULTY_LABELS"):
        start = src.index(f"{name} = frozenset")
        end = src.index(")", src.index("}", start)) + 1
        exec(compile(src[start:end], "<canonical>", "exec"), ns)  # noqa: S102
    assert ns["_AREA_LABELS"] == fti.AREA_LABELS
    assert ns["_ENV_DIFFICULTY_LABELS"] == fti.DIFFICULTY_LABELS


# --- round 2: issue text was interpolated into a shell command line ----------


def test_title_with_shell_metacharacters_is_passed_as_argv_verbatim():
    """The whole reason this is a script: no shell, so nothing can execute."""
    calls: list = []
    run = _runner({"issue create": _proc("https://example/1")}, calls)
    nasty = "Fix `$(id -un)` and $(rm -rf /) parsing; use \"quotes\" & 'apostrophes'"
    fti.create_issue("Org/Repo", nasty, "/tmp/b.md", ["area:memory", "help wanted"], run)
    argv = calls[0]
    assert nasty in argv, "title must reach gh as a single unmangled argv element"
    assert argv[argv.index("--title") + 1] == nasty


def test_multiword_labels_are_separate_argv_elements():
    """`good first issue` word-splits if it ever touches a shell."""
    calls: list = []
    run = _runner({"issue create": _proc("https://example/1")}, calls)
    fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "good first issue"], run)
    assert "good first issue" in calls[0]


def test_repo_is_pinned_on_every_gh_invocation():
    """A bare gh command re-resolves its target from the cwd."""
    calls: list = []
    run = _runner(
        {
            "issue list": _proc("[]"),
            "issue create": _proc("https://example/1"),
        },
        calls,
    )
    fti.find_duplicate("Org/Repo", "t", run)
    fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)
    for argv in calls:
        assert "--repo" in argv and argv[argv.index("--repo") + 1] == "Org/Repo"


# --- end to end through main() ----------------------------------------------


def _drafts(tmp_path: Path, title: str = "A real title") -> tuple[str, str]:
    t = tmp_path / "title.txt"
    b = tmp_path / "body.md"
    t.write_text(title, encoding="utf-8")
    b.write_text("body text", encoding="utf-8")
    return str(t), str(b)


def test_main_dry_run_passes_every_check_without_posting(tmp_path, capsys):
    calls: list = []
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
            "issue list": _proc("[]"),
        },
        calls,
    )
    t, b = _drafts(tmp_path)
    rc = fti.main(
        [
            "--title-file",
            t,
            "--body-file",
            b,
            "--area",
            "area:memory",
            "--difficulty",
            "help wanted",
            "--dry-run",
        ],
        run,
    )
    assert rc == 0
    assert "DRY RUN ok" in capsys.readouterr().out
    assert not any("issue create" in " ".join(c) for c in calls), "dry run must not post"


def test_main_refuses_on_a_fork_without_write_access(tmp_path, capsys):
    run = _runner(
        {
            "isFork": _proc(
                json.dumps(
                    {
                        "isFork": True,
                        "nameWithOwner": "downstream/Proj",
                        "parent": {"owner": {"login": "Upstream"}, "name": "Proj"},
                    }
                )
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "READ"})),
        }
    )
    t, b = _drafts(tmp_path)
    rc = fti.main(
        [
            "--title-file",
            t,
            "--body-file",
            b,
            "--area",
            "area:memory",
            "--difficulty",
            "help wanted",
        ],
        run,
    )
    assert rc == 2
    assert "no write access" in capsys.readouterr().err


def test_main_reports_duplicate_and_files_nothing(tmp_path, capsys):
    calls: list = []
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "WRITE"})),
            "issue list": _proc(json.dumps([{"number": 9, "title": "A real title"}])),
        },
        calls,
    )
    t, b = _drafts(tmp_path)
    rc = fti.main(
        [
            "--title-file",
            t,
            "--body-file",
            b,
            "--area",
            "area:other",
            "--difficulty",
            "help wanted",
        ],
        run,
    )
    assert rc == 3
    assert "DUPLICATE: Org/Repo#9" in capsys.readouterr().err
    assert not any("issue create" in " ".join(c) for c in calls)


def test_main_refuses_an_empty_title(tmp_path, capsys):
    t = tmp_path / "title.txt"
    t.write_text("   \n", encoding="utf-8")
    b = tmp_path / "body.md"
    b.write_text("body", encoding="utf-8")
    rc = fti.main(
        [
            "--title-file",
            str(t),
            "--body-file",
            str(b),
            "--area",
            "area:memory",
            "--difficulty",
            "help wanted",
        ],
        _runner({}),
    )
    assert rc == 2
    assert "title file is empty" in capsys.readouterr().err
