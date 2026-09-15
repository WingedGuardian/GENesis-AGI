"""Tests for scripts/file_tracker_issue.py.

Every test here corresponds to a defect a cross-model reviewer found in the
PROSE version of this procedure across three rounds. The point of moving it into
a script was that these become checkable; the point of these tests is that the
class cannot come back silently.

No network, no `gh`, no live repo — the runner is injected.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "file_tracker_issue.py"
_spec = importlib.util.spec_from_file_location("file_tracker_issue", _SCRIPT)
assert _spec and _spec.loader
fti = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fti)



class _PathWithFailingMkdir(Path):
    """Scoped stand-in: only THIS class fails, not pathlib.Path process-wide."""

    def mkdir(self, *a, **k):
        raise OSError("read-only file system")


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


def test_create_failure_reconciles_against_the_tracker():
    """Superseded round-4 behaviour: the outcome is RESOLVED, not reported.

    Kept as a test because the guarantee still matters — a lost response after
    the server committed means the issue EXISTS — but the script now answers the
    question instead of handing it to the operator.
    """

    def run(argv):
        if "issue create" in " ".join(argv):
            return _proc(returncode=1, stderr="connection reset")
        return _proc("")

    with pytest.raises(fti.Refused, match="Reconciled against"):
        fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)


def test_indeterminate_is_not_a_refused_subclass():
    """Refused's contract is 'nothing was posted'. Conflating them is the bug."""
    assert not issubclass(fti.Indeterminate, fti.Refused)
    assert not issubclass(fti.Refused, fti.Indeterminate)


def test_main_reports_exit_4_for_an_indeterminate_post(tmp_path, capsys):
    """Exit 4 now means only one thing: the reconciling lookup ALSO failed."""
    calls = {"api": 0}

    def run(argv):
        joined = " ".join(argv)
        if "isFork" in joined:
            return _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            )
        if "viewerPermission" in joined:
            return _proc(json.dumps({"viewerPermission": "ADMIN"}))
        if "gh api" in joined:
            calls["api"] += 1
            # first call = the pre-flight dup check; second = the reconcile
            return _proc("") if calls["api"] == 1 else _proc(returncode=1, stderr="network down")
        if "issue create" in joined:
            return _proc(returncode=1, stderr="connection reset")
        raise AssertionError(joined)

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
    assert calls["api"] == 2, "must have attempted a reconcile"


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
    monkeypatch.setattr(fti, "Path", type("P", (fti.Path.__class__ if False else Path,), {}))
    monkeypatch.setattr(fti, "_lock_base_override", tmp_path, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
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


# ===========================================================================
# round 5 — same class again, at the outer edges: the script inferring whether
# it posted. Resolved by asking the TRACKER instead of the exit status.
# ===========================================================================


def test_create_timeout_is_reconciled_not_an_uncaught_exception():
    """A timeout is EXACTLY when the post may exist — it must not escape."""
    calls: list = []

    def run(argv):
        calls.append(list(argv))
        if "issue create" in " ".join(argv):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=120)
        return _proc(json.dumps({"number": 77, "title": "A real title"}))

    out = fti.create_issue(
        "Org/Repo", "A real title", "/tmp/b.md", ["area:eval", "help wanted"], run
    )
    assert "Org/Repo#77" in out
    assert "EXISTS" in out
    assert any("gh api" in " ".join(c) for c in calls), "must reconcile against the tracker"


def test_timeout_with_no_issue_on_the_tracker_is_a_clean_refusal():
    """Reconciling can also prove the post did NOT happen — say so definitively."""

    def run(argv):
        if "issue create" in " ".join(argv):
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=120)
        return _proc("")

    with pytest.raises(fti.Refused, match="nothing was posted"):
        fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)


def test_nonzero_exit_with_the_issue_present_reports_success():
    """The old code called this 'indeterminate'; the tracker knows the answer."""

    def run(argv):
        if "issue create" in " ".join(argv):
            return _proc(returncode=1, stderr="connection reset")
        return _proc(json.dumps({"number": 42, "title": "A real title"}))

    out = fti.create_issue(
        "Org/Repo", "A real title", "/tmp/b.md", ["area:eval", "help wanted"], run
    )
    assert "Org/Repo#42" in out


def test_indeterminate_only_when_the_reconciling_lookup_also_fails():
    """The one genuinely unknowable case — and the only one left."""

    def run(argv):
        if "issue create" in " ".join(argv):
            return _proc(returncode=1, stderr="connection reset")
        return _proc(returncode=1, stderr="network down")

    with pytest.raises(fti.Indeterminate, match="MAY exist"):
        fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)


def test_lock_directory_failure_refuses_rather_than_using_a_tmpdir_fallback(monkeypatch):
    """A TMPDIR-dependent fallback is not a lock: CC sets TMPDIR, a shell does not."""


    monkeypatch.setattr(fti, "Path", _PathWithFailingMkdir)
    with pytest.raises(fti.Refused, match="TMPDIR-dependent fallback"):
        fti._lock_path("Org/Repo")


def test_no_tempfile_fallback_remains_in_the_source():
    """Guard the guard: the fallback must be gone, not merely unreachable.

    Asserts on the IMPORT, not on the word — the docstring deliberately names
    `tempfile.gettempdir()` to explain why the fallback was removed, and a guard
    that fires on its own rationale is one the next reader deletes.
    """
    src = (
        Path(__file__).resolve().parents[2] / "scripts" / "file_tracker_issue.py"
    ).read_text()
    tree = ast.parse(src)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "tempfile" not in imported, "cannot call gettempdir() without importing it"


def test_a_reporting_failure_does_not_reclassify_a_posted_issue(tmp_path, capsys, monkeypatch):
    """In-process half of the guarantee; the exit-code half is the subprocess test below.

    Kept deliberately narrow: this proves main() RETURNS 0 when the URL cannot be
    written. It cannot prove the PROCESS exits 0 — the shutdown flush happens
    after main() returns — which is why the subprocess test exists and why the
    version of this test that only did the former was false assurance.
    """
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
            "gh api": _proc(""),
            "issue create": _proc("https://example/1"),
        }
    )
    real_print = print
    state = {"failed": False}

    def flaky(*a, **k):
        if not state["failed"] and a and str(a[0]).startswith("https://"):
            state["failed"] = True
            raise BrokenPipeError("stdout closed")
        return real_print(*a, **k)

    monkeypatch.setattr("builtins.print", flaky)
    tf, bf = _drafts(tmp_path)
    rc = fti.main(
        ["--title-file", tf, "--body-file", bf, "--area", "area:memory",
         "--difficulty", "help wanted"],
        run,
    )
    assert rc == 0, "a confirmed post must not be reported as failure"
    assert state["failed"], "the print failure must actually have been exercised"
    assert "POSTED" in capsys.readouterr().err


# ===========================================================================
# fresh-context audit — the exception surface. Each of these is a type that
# escaped EVERY handler, so the documented exit-code contract was not real.
# ===========================================================================


def test_reconcile_timeout_does_not_escape_as_exit_1():
    """TimeoutExpired is not an OSError — it was caught nowhere on this path."""

    def run(argv):
        if "issue create" in " ".join(argv):
            return _proc(returncode=1, stderr="connection reset")
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=120)

    # the reconcile's own lookup timing out is Refused (nothing proven posted),
    # never an uncaught exception
    with pytest.raises(fti.Indeterminate):
        fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)


def test_preflight_duplicate_timeout_is_refused_not_uncaught():
    def run(argv):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=120)

    with pytest.raises(fti.Refused, match="did not complete"):
        fti.find_duplicate("Org/Repo", "t", run)


def test_interrupt_during_create_reconciles():
    """KeyboardInterrupt is a BaseException — it escapes `except Exception`."""

    def run(argv):
        if "issue create" in " ".join(argv):
            raise KeyboardInterrupt
        return _proc(json.dumps({"number": 5, "title": "t"}))

    out = fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)
    assert "Org/Repo#5" in out


def test_gh_returning_a_non_object_is_refused_not_attributeerror():
    run = _runner({"isFork": _proc("[]")})
    with pytest.raises(fti.Refused, match="expected an object"):
        fti.resolve_tracker(run)


def test_rc_zero_with_no_url_is_reconciled_not_reported_as_filed():
    """Success with nothing to cite is the same false certainty, inverted."""

    def run(argv):
        if "issue create" in " ".join(argv):
            return _proc("")
        return _proc("")

    with pytest.raises(fti.Refused, match="printed no URL"):
        fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)


def test_unreadable_or_non_utf8_draft_is_exit_2_not_1(tmp_path, capsys):
    """UnicodeDecodeError is a ValueError — no handler caught it."""
    t = tmp_path / "title.txt"
    t.write_bytes(b"\xff\xfe not utf-8")
    b = tmp_path / "body.md"
    b.write_text("body", encoding="utf-8")
    rc = fti.main(
        ["--title-file", str(t), "--body-file", str(b), "--area", "area:memory",
         "--difficulty", "help wanted"],
        _runner({}),
    )
    assert rc == 2, "a draft we cannot read means nothing was posted"
    assert "cannot read the draft files" in capsys.readouterr().err


def test_missing_title_file_is_exit_2(tmp_path, capsys):
    b = tmp_path / "body.md"
    b.write_text("body", encoding="utf-8")
    rc = fti.main(
        ["--title-file", str(tmp_path / "nope.txt"), "--body-file", str(b),
         "--area", "area:memory", "--difficulty", "help wanted"],
        _runner({}),
    )
    assert rc == 2
    assert "cannot read the draft files" in capsys.readouterr().err


def test_over_long_and_multiline_titles_refuse_before_any_network_call(tmp_path):
    """A title GitHub will reject would otherwise loop forever on 'safe to retry'."""
    b = tmp_path / "body.md"
    b.write_text("body", encoding="utf-8")

    def run(argv):
        raise AssertionError("must refuse before touching the network")

    for bad in ("x" * 257, "line one\nline two"):
        t = tmp_path / "title.txt"
        t.write_text(bad, encoding="utf-8")
        rc = fti.main(
            ["--title-file", str(t), "--body-file", str(b), "--area", "area:memory",
             "--difficulty", "help wanted"],
            run,
        )
        assert rc == 2


# --- the exit code must survive interpreter shutdown ------------------------


def test_confirmed_post_does_not_exit_120_when_stdout_is_full(tmp_path):
    """The real defect: CPython's shutdown flush overrides the return code.

    Runs the script as a SUBPROCESS with stdout on /dev/full, because the
    previous version of this test injected a `print` that raises — which the
    real `print` does not — and passed green while the script exited 120.
    Asserting on the process's actual exit status is the only way to see it.
    """
    stub = tmp_path / "gh"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"--json isFork"*) echo \'{"isFork":false,"nameWithOwner":"Org/Repo","parent":null}\' ;;\n'
        '  *viewerPermission*) echo \'{"viewerPermission":"ADMIN"}\' ;;\n'
        '  *"api"*) : ;;\n'
        '  *"issue create"*) echo "https://example/1" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    title = tmp_path / "title.txt"
    title.write_text("A real title", encoding="utf-8")
    body = tmp_path / "body.md"
    body.write_text("body", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", HOME=str(home))
    with open("/dev/full", "w", encoding="utf-8") as full:
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--title-file", str(title), "--body-file",
             str(body), "--area", "area:memory", "--difficulty", "help wanted"],
            stdout=full, stderr=subprocess.PIPE, text=True, timeout=60, check=False,
            env=env,
        )
    assert proc.returncode != 120, (
        "a CONFIRMED post exited 120 — CPython's shutdown flush overrode the exit code"
    )
    assert proc.returncode == 0, f"expected 0 for a successful post, got {proc.returncode}"


# ===========================================================================
# round 6 — the guarantee's own edges. Each of these is a hole IN the fix that
# was written to close the previous hole.
# ===========================================================================


def test_finish_survives_stdout_being_none():
    """`>&-` makes sys.stdout None; the guard itself raised AttributeError."""
    real = sys.stdout
    try:
        sys.stdout = None
        assert fti._finish(0) == 0
        assert fti._finish(4) == 4
    finally:
        sys.stdout = real


def test_finish_survives_an_already_closed_stream():
    """flush() raises ValueError, and so does the fileno() used to recover."""

    class Closed:
        closed = True

        def flush(self):
            raise ValueError("I/O operation on closed file")

        def fileno(self):
            raise ValueError("I/O operation on closed file")

    real = sys.stdout
    try:
        sys.stdout = Closed()
        assert fti._finish(0) == 0
    finally:
        sys.stdout = real


def test_finish_never_raises_for_any_stdout_shape():
    """It IS the exit-code guarantee, so it must be total."""

    class Hostile:
        def flush(self):
            raise RuntimeError("unexpected")

        def fileno(self):
            raise RuntimeError("unexpected")

    real = sys.stdout
    try:
        for stream in (None, Hostile()):
            sys.stdout = stream
            assert fti._finish(7) == 7
    finally:
        sys.stdout = real


def test_a_late_interrupt_after_creation_reports_the_post_not_a_refusal(tmp_path, capsys):
    """SIGINT while releasing the lock is AFTER the post — exit 0, not 2."""

    class InterruptingLock:
        def __init__(self, slug):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            raise KeyboardInterrupt

    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
            "gh api": _proc(""),
            "issue create": _proc("https://example/9"),
        }
    )
    import unittest.mock

    with unittest.mock.patch.object(fti, "tracker_lock", InterruptingLock):
        tf, bf = _drafts(tmp_path)
        rc = fti.main(
            ["--title-file", tf, "--body-file", bf, "--area", "area:memory",
             "--difficulty", "help wanted"],
            run,
        )
    assert rc == 0, "the issue exists; reporting a refusal invites a duplicate retry"
    err = capsys.readouterr().err
    assert "POSTED" in err and "https://example/9" in err


def test_reconcile_failing_to_start_gh_is_indeterminate_not_exit_1():
    """FileNotFoundError is an OSError — it bypassed the Refused-only clause."""

    def run(argv):
        if "issue create" in " ".join(argv):
            return _proc(returncode=1, stderr="connection reset")
        raise FileNotFoundError("gh: no such file")

    with pytest.raises(fti.Indeterminate):
        fti.create_issue("Org/Repo", "t", "/tmp/b.md", ["area:eval", "help wanted"], run)


def test_dry_run_is_finalized_through_finish(tmp_path, monkeypatch):
    """Every return path must be finalized, or it exits 120 on a bad stdout."""
    seen = {}
    real_finish = fti._finish

    def spy(code):
        seen["code"] = code
        return real_finish(code)

    monkeypatch.setattr(fti, "_finish", spy)
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
            "gh api": _proc(""),
        }
    )
    tf, bf = _drafts(tmp_path)
    rc = fti.main(
        ["--title-file", tf, "--body-file", bf, "--area", "area:memory",
         "--difficulty", "help wanted", "--dry-run"],
        run,
    )
    assert rc == 0
    assert seen.get("code") == 0, "the dry-run path bypassed the exit-code guarantee"


def test_duplicate_path_is_finalized_through_finish(tmp_path, monkeypatch):
    seen = {}
    real_finish = fti._finish
    monkeypatch.setattr(fti, "_finish", lambda c: (seen.setdefault("code", c), real_finish(c))[1])
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
            "gh api": _proc(json.dumps({"number": 3, "title": "A real title"})),
        }
    )
    tf, bf = _drafts(tmp_path)
    rc = fti.main(
        ["--title-file", tf, "--body-file", bf, "--area", "area:memory",
         "--difficulty", "help wanted"],
        run,
    )
    assert rc == 3
    assert seen.get("code") == 3


# --- round 7: the last three -------------------------------------------------


def test_stderr_is_finalized_too(monkeypatch):
    """CPython flushes stderr at shutdown as well — it can also force exit 120."""
    flushed = []

    class S:
        def __init__(self, name):
            self.name = name

        def flush(self):
            flushed.append(self.name)

        def fileno(self):
            return 1

    monkeypatch.setattr(sys, "stdout", S("out"))
    monkeypatch.setattr(sys, "stderr", S("err"))
    assert fti._finish(2) == 2
    assert flushed == ["out", "err"], f"both streams must be finalized, got {flushed}"


def test_interrupt_while_creating_is_indeterminate_not_refused(tmp_path, capsys):
    """Between attempting the create and it returning, the outcome is UNKNOWN."""
    run = _runner(
        {
            "isFork": _proc(
                json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
            ),
            "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
            "gh api": _proc(""),
        }
    )

    def interrupting(*a, **k):
        raise KeyboardInterrupt

    import unittest.mock

    with unittest.mock.patch.object(fti, "create_issue", interrupting):
        tf, bf = _drafts(tmp_path)
        rc = fti.main(
            ["--title-file", tf, "--body-file", bf, "--area", "area:memory",
             "--difficulty", "help wanted"],
            run,
        )
    assert rc == 4, "the request may have reached GitHub — refusing would invite a duplicate"
    assert "INDETERMINATE" in capsys.readouterr().err


class TestSigtermHandlerIsScoped:
    """main() must not leave the SIGTERM handler installed process-wide.

    REGRESSION (MEASURED in CI): main() installed `_sigterm_as_interrupt`
    globally and never restored it. These tests call main() in-process, so the
    handler outlived them and was inherited by every process forked later in the
    same pytest run. tests/test_scripts/test_session_heartbeat_throttle.py forks
    multiprocessing children and terminate()s them; the inherited handler turned
    that SIGTERM into a KeyboardInterrupt instead of death, join() never
    returned, and TWO tests in a file this PR does not touch hung for the full
    1800s pytest-timeout. Test files run alphabetically, so this one poisoned it.
    """

    @staticmethod
    def _ok_runner(calls=None):
        return _runner(
            {
                "isFork": _proc(
                    json.dumps({"isFork": False, "nameWithOwner": "Org/Repo", "parent": None})
                ),
                "viewerPermission": _proc(json.dumps({"viewerPermission": "ADMIN"})),
                "gh api": _proc(""),
            },
            calls,
        )

    @staticmethod
    def _argv(tmp_path):
        t_, b_ = _drafts(tmp_path)
        return [
            "--title-file", t_,
            "--body-file", b_,
            "--area", "area:memory",
            "--difficulty", "help wanted",
            "--dry-run",
        ]

    def test_main_restores_the_previous_sigterm_handler(self, tmp_path, capsys):
        """The leak itself: after main() returns, the handler is what it was.

        A DISTINCTIVE sentinel is installed first rather than reading whatever
        happens to be current. Reading the current handler makes this test
        order-dependent and nearly vacuous: any earlier test in this file that
        calls main() would already have leaked the handler, so `before` and
        `after` would both be the leaked one and compare equal. MEASURED — that
        weaker form passed against the unfixed script.
        """

        def _sentinel(signum, frame):  # pragma: no cover - never delivered
            raise AssertionError("sentinel handler should not run")

        prior = signal.signal(signal.SIGTERM, _sentinel)
        try:
            rc = fti.main(self._argv(tmp_path), self._ok_runner())
            capsys.readouterr()

            assert rc == 0
            assert signal.getsignal(signal.SIGTERM) is _sentinel
        finally:
            signal.signal(signal.SIGTERM, prior)

    def test_the_handler_is_active_while_main_runs(self, tmp_path, capsys):
        """The guarantee the scope must not cost us — untested until now.

        Restoring is only correct if the handler is genuinely installed FOR the
        duration. A "fix" that simply stopped installing it would pass the leak
        test above while silently dropping the reconcile-on-SIGTERM property the
        handler exists for, so both halves are pinned.
        """
        seen = []
        inner = self._ok_runner()

        def watching_run(argv):
            seen.append(signal.getsignal(signal.SIGTERM))
            return inner(argv)

        rc = fti.main(self._argv(tmp_path), watching_run)
        capsys.readouterr()

        assert rc == 0
        assert seen, "the runner was never called, so nothing was observed"
        assert all(h is fti._sigterm_as_interrupt for h in seen)
