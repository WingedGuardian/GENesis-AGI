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

    def run(argv):
        if "Upstream/Proj" in " ".join(argv):
            return _proc(
                json.dumps(
                    {"isFork": False, "nameWithOwner": "Upstream/Proj", "parent": None}
                )
            )
        return _proc(
            json.dumps(
                {
                    "isFork": True,
                    "nameWithOwner": "downstream/Proj",
                    "parent": {"owner": {"login": "Upstream"}, "name": "Proj"},
                }
            )
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
    run = _runner({"gh api": _proc('{"number":5,"title":"unrelated"}')}, calls)
    fti.find_duplicate("Org/Repo", "fix repo: parsing in is: handler", run)
    assert "--search" not in " ".join(calls[0])


def test_exact_title_match_is_found_and_near_miss_is_not():
    listing = (
        json.dumps({"number": 11, "title": "Fix   the   PARSER  "})
        + "\n"
        + json.dumps({"number": 12, "title": "Fix the parser eventually"})
    )
    run = _runner({"gh api": _proc(listing)})
    assert fti.find_duplicate("Org/Repo", "fix the parser", run) == 11
    assert fti.find_duplicate("Org/Repo", "totally different title", run) is None


def test_failed_duplicate_lookup_refuses_rather_than_assuming_none():
    """A failed lookup is not evidence of no duplicate."""
    run = _runner({"gh api": _proc(returncode=1, stderr="network down")})
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
            "gh api": _proc(""),
            "issue create": _proc("https://example/1"),
        },
        calls,
    )
    fti.find_duplicate("Org/Repo", "t", run)
    fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)
    for argv in calls:
        joined = " ".join(argv)
        if "--repo" in argv:
            assert argv[argv.index("--repo") + 1] == "Org/Repo"
        else:
            assert "repos/Org/Repo/" in joined, "the slug must be pinned in the API path"


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
            "gh api": _proc(""),
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
    def run(argv):
        joined = " ".join(argv)
        if "viewerPermission" in joined:
            return _proc(json.dumps({"viewerPermission": "READ"}))
        if "Upstream/Proj" in joined:
            return _proc(
                json.dumps(
                    {"isFork": False, "nameWithOwner": "Upstream/Proj", "parent": None}
                )
            )
        return _proc(
            json.dumps(
                {
                    "isFork": True,
                    "nameWithOwner": "downstream/Proj",
                    "parent": {"owner": {"login": "Upstream"}, "name": "Proj"},
                }
            )
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
            "gh api": _proc(json.dumps({"number": 9, "title": "A real title"})),
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


# ===========================================================================
# round 4 (Codex, on the script itself) — one class: CLAIMING MORE CERTAINTY
# THAN THE STEP ACTUALLY HAS.
# ===========================================================================


def test_fork_of_a_fork_walks_to_the_network_root():
    """One `parent` hop is not the root — the intermediate fork may be writable."""
    seen = []

    def run(argv):
        seen.append(list(argv))
        joined = " ".join(argv)
        if "mid/Proj" in joined:
            return _proc(
                json.dumps(
                    {
                        "isFork": True,
                        "nameWithOwner": "mid/Proj",
                        "parent": {"owner": {"login": "Root"}, "name": "Proj"},
                    }
                )
            )
        if "Root/Proj" in joined:
            return _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Root/Proj", "parent": None})
            )
        return _proc(
            json.dumps(
                {
                    "isFork": True,
                    "nameWithOwner": "leaf/Proj",
                    "parent": {"owner": {"login": "mid"}, "name": "Proj"},
                }
            )
        )

    assert fti.resolve_tracker(run) == "Root/Proj"
    assert len(seen) == 3, "must keep walking while isFork, not stop at the first parent"


def test_fork_chain_deeper_than_the_bound_refuses():
    """A cycle or a misbehaving API must not spin."""

    def run(argv):
        return _proc(
            json.dumps(
                {
                    "isFork": True,
                    "nameWithOwner": "a/Proj",
                    "parent": {"owner": {"login": "b"}, "name": "Proj"},
                }
            )
        )

    with pytest.raises(fti.Refused, match="fork chain deeper"):
        fti.resolve_tracker(run)


def test_duplicate_lookup_paginates_and_is_not_capped():
    """A capped window that excludes the match is indistinguishable from no match."""
    calls: list = []
    run = _runner({"gh api": _proc('{"number":1,"title":"other"}\n')}, calls)
    fti.find_duplicate("Org/Repo", "anything", run)
    argv = " ".join(calls[0])
    assert "--paginate" in argv, "must walk every page, not a first window"
    assert "--limit" not in argv, "no cap — absence from a truncated read is not absence"
    assert "pull_request" in argv, "GitHub's issues endpoint returns PRs too"


def test_duplicate_found_on_a_later_page():
    """The match is the LAST record — a capped read would have missed it."""
    lines = "\n".join(
        json.dumps({"number": n, "title": f"filler {n}"}) for n in range(1, 300)
    )
    lines += "\n" + json.dumps({"number": 999, "title": "The Real Title"})
    run = _runner({"gh api": _proc(lines)})
    assert fti.find_duplicate("Org/Repo", "the real title", run) == 999


def test_partial_json_in_the_listing_refuses_rather_than_reporting_absence():
    run = _runner({"gh api": _proc('{"number":1,"title":"ok"}\n{"number":2,"tit')})
    with pytest.raises(fti.Refused, match="cannot prove absence"):
        fti.find_duplicate("Org/Repo", "anything", run)


def test_create_failure_is_indeterminate_not_refused():
    """A lost response after the server committed means the issue EXISTS."""
    run = _runner({"issue create": _proc(returncode=1, stderr="connection reset")})
    with pytest.raises(fti.Indeterminate, match="MAY have been created"):
        fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)


def test_indeterminate_is_not_a_refused_subclass():
    """Refused's contract is 'nothing was posted'. Conflating them is the bug."""
    assert not issubclass(fti.Indeterminate, fti.Refused)
    assert not issubclass(fti.Refused, fti.Indeterminate)


def test_main_reports_exit_4_for_an_indeterminate_post(tmp_path, capsys):
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
            "gh api": _proc(""),
            "issue create": _proc(returncode=1, stderr="connection reset"),
        }
    )
    t, b = _drafts(tmp_path)
    rc = fti.main(
        ["--title-file", t, "--body-file", b, "--area", "area:memory",
         "--difficulty", "help wanted"],
        run,
    )
    assert rc == 4, "exit 2 would tell the operator nothing was posted — it may have been"
    err = capsys.readouterr().err
    assert "INDETERMINATE" in err
    assert "BEFORE retrying" in err


def test_lookup_and_create_run_inside_the_tracker_lock(tmp_path, monkeypatch):
    """Check-then-act must be one critical section, or both sessions post."""
    events: list = []

    class _Recorder:
        def __init__(self, slug):
            self.slug = slug

        def __enter__(self):
            events.append("lock")
            return self

        def __exit__(self, *a):
            events.append("unlock")
            return False

    monkeypatch.setattr(fti, "tracker_lock", _Recorder)

    def run(argv):
        joined = " ".join(argv)
        if "isFork" in joined:
            return _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            )
        if "viewerPermission" in joined:
            return _proc(json.dumps({"viewerPermission": "ADMIN"}))
        if "gh api" in joined:
            events.append("lookup")
            return _proc("")
        if "issue create" in joined:
            events.append("create")
            return _proc("https://example/1")
        raise AssertionError(joined)

    t, b = _drafts(tmp_path)
    rc = fti.main(
        ["--title-file", t, "--body-file", b, "--area", "area:memory",
         "--difficulty", "help wanted"],
        run,
    )
    assert rc == 0
    assert events == ["lock", "lookup", "create", "unlock"], events


def test_tracker_lock_is_per_tracker_and_actually_excludes(tmp_path, monkeypatch):
    """Different trackers get different locks; the same tracker serialises."""
    monkeypatch.setattr(fti.Path, "home", staticmethod(lambda: tmp_path))
    a1 = fti._lock_path("Org/Repo")
    a2 = fti._lock_path("Org/Repo")
    b1 = fti._lock_path("Other/Repo")
    assert a1 == a2 and a1 != b1

    import fcntl as _f

    with (
        fti.tracker_lock("Org/Repo"),
        open(a1, "w", encoding="utf-8") as second,
        pytest.raises(BlockingIOError),
    ):
        _f.flock(second, _f.LOCK_EX | _f.LOCK_NB)
