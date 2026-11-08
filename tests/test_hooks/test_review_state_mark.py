"""Tests for the review marker CLI (`scripts/review_state.py mark`).

Pins what `mark` still does: it writes the per-worktree review marker, records
the review `source` (internal / external) and, for an external review, its
`outcome` (clean / defects), and refuses fail-closed on a malformed invocation
(no marker written). Nothing here counts rounds; `mark` prints no round, streak
or reminder text. Also holds the identity locks on the shared `review_state` and
`review_scope` module names that monkeypatching tests elsewhere rest on.

Hermetic: throwaway git repo under ``tmp_path`` with ``HOME`` pointed at another
temp dir, so the real per-worktree markers under ``~/.genesis/`` are never
touched. No network, no live server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_STATE = _REPO_ROOT / "scripts" / "review_state.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
import review_scope  # noqa: E402
import review_state  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _stage(repo: Path, content: str) -> None:
    (repo / "f.py").write_text(content)
    _git(repo, "add", "-A")


def _marker(repo: Path, home: Path) -> Path:
    key = review_state._worktree_key(cwd=str(repo))
    return home / ".genesis" / "review_markers" / f"{key}.json"


def _mark_raw(repo: Path, home: Path, *extra: str) -> subprocess.CompletedProcess:
    """Run the `mark` CLI with an explicit arg list (no flag injected)."""
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    args = [
        sys.executable,
        str(_REVIEW_STATE),
        "mark",
        "--agent-output",
        str(home / ".genesis" / "last_code_review.txt"),
        *extra,
    ]
    return subprocess.run(args, cwd=str(repo), env=env, capture_output=True, text=True, timeout=30)


def test_the_shared_review_state_name_is_not_hijacked():
    """The premise every `monkeypatch.setattr(review_state, ...)` rests on.

    `review_enforcement_commit` imports `review_state` at CALL time, so a patch
    applied in a test module only reaches production code if that module's
    `review_state` and `sys.modules["review_state"]` are the SAME object.

    Other test modules load a private copy of that script under the shared name.
    pytest imports every test module at COLLECTION, so the last registration
    wins for the whole session: a module collected earlier keeps a reference to
    the object it bound, while production resolves whatever is in `sys.modules`
    now. The patch then lands on nobody and the real function runs — a failure
    invisible in a single-file run and visible only in the full suite, in
    collection order.

    Asserting the premise directly means the next unrestored hijack fails HERE,
    naming the cause, instead of surfacing as a bewildering assertion in an
    unrelated test. `tests.conftest.private_module` is the supported way to load
    a private copy without leaking the name.
    """
    assert sys.modules.get("review_state") is review_state, (
        "sys.modules['review_state'] is not the module this file imported — "
        "some test module loaded a private copy under the shared name and did "
        "not restore it, so monkeypatching this module patches nothing that "
        "production code will resolve. Load it via tests.conftest.private_module."
    )


def test_the_shared_review_scope_name_is_not_hijacked():
    """The sibling lock, for the second name the commit gate imports at call time.

    `review_scope` is resolved by a call-time import in several places, including
    `review_enforcement_commit.classify_change_substantiality`, so the premise is
    the same one the `review_state` lock above rests on: a patch applied to the
    object a test holds only reaches production if it is the object `sys.modules`
    hands the call-time import.

    This file binds `review_scope` at module scope purely so that identity exists
    to compare against. PATH equality is not enough: two distinct module objects
    loaded from the SAME path compare equal by path and differ by identity, so a
    private copy left registered would pass a path assert while a
    `monkeypatch.setattr` on the canonical object reached nobody —
    `tests/test_scripts/test_check_review_depth.py` patches
    `review_scope.classify_range_substantiality` against a call-time import.
    """
    assert sys.modules.get("review_scope") is review_scope, (
        "sys.modules['review_scope'] is not the module this file imported — "
        "some test module loaded a private copy under the shared name and did "
        "not restore it. Same path is NOT sufficient: a same-path copy is a "
        "different object, so patches land on one and production resolves the "
        "other. Load it via tests.conftest.private_module."
    )


# ── The `mark` CLI ─────────────────────────────────────────────────────────
#
# `--source internal` (the default) needs no outcome flag; `--source external`
# requires exactly one of --clean / --defects. Every refusal is fail-closed: exit
# 1 and NO marker written, so the commit gate still blocks until a correct re-run.


def test_internal_mark_needs_no_outcome(repo, home):
    # A bare `mark` (default --source internal, no outcome flag) is a VALID review:
    # the marker is written and nothing about rounds or streaks is printed.
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home)  # no --source, no outcome
    assert res.returncode == 0, res.stdout + res.stderr
    assert _marker(repo, home).exists()
    assert "(internal review)" in res.stdout
    out = (res.stdout + res.stderr).lower()
    assert "round" not in out and "streak" not in out


def test_external_mark_requires_explicit_outcome(repo, home):
    # An `--source external` mark WITHOUT an outcome flag is REFUSED: no marker, exit 1.
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source", "external")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "outcome" in res.stderr.lower()
    assert not _marker(repo, home).exists()


def test_mark_rejects_invalid_source(repo, home):
    # An unrecognized --source is refused (not silently coerced at the CLI boundary).
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source", "anthropic", "--defects")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "source" in res.stderr.lower()
    assert not _marker(repo, home).exists()


def test_mark_rejects_valueless_source(repo, home):
    # A valueless --source (the flag as the LAST token) must be REFUSED — NOT silently
    # defaulted to internal, which would record an intended external review as internal.
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--defects", "--source")  # --source has no value
    assert res.returncode == 1, res.stdout + res.stderr
    assert "source" in res.stderr.lower()
    assert not _marker(repo, home).exists()


def test_source_equals_form_is_parsed(repo, home):
    # `--source=external` must parse like `--source external`, not be silently dropped
    # to the internal default. The equals form of an invalid value is still rejected.
    marker = _marker(repo, home)
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source=external", "--defects")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads(marker.read_text())["source"] == "external"
    _stage(repo, "a = 3\n")
    res2 = _mark_raw(repo, home, "--source=bogus", "--defects")
    assert res2.returncode == 1 and "source" in res2.stderr.lower()


def test_mark_fails_closed_on_unknown_option_typo(repo, home):
    # A typo'd flag (e.g. `--soruce`) must FAIL CLOSED, not be silently skipped —
    # otherwise the internal default stands and an intended external review is
    # recorded internal. No marker.
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--soruce", "external", "--defects")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "unknown option" in res.stderr.lower()
    assert not _marker(repo, home).exists()


def test_mark_rejects_contradictory_outcome(repo, home):
    # Passing BOTH --clean and --defects is a contradiction → refused, no marker.
    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home, "--source", "external", "--clean", "--defects")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "both" in res.stderr.lower()
    assert not _marker(repo, home).exists()


def test_marker_carries_source_and_outcome(repo, home):
    # Observability: the marker records who reviewed and, for an external review,
    # what it found. An internal review has no outcome.
    marker = _marker(repo, home)

    _stage(repo, "a = 2\n")
    res = _mark_raw(repo, home)
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(marker.read_text())
    assert data["source"] == "internal"
    assert data["outcome"] is None

    _stage(repo, "a = 3\n")
    res = _mark_raw(repo, home, "--source", "external", "--clean")
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(marker.read_text())
    assert data["source"] == "external"
    assert data["outcome"] == "clean"
    assert "(external review, clean)" in res.stdout

    _stage(repo, "a = 4\n")
    res = _mark_raw(repo, home, "--source", "external", "--defects")
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(marker.read_text())
    assert data["source"] == "external"
    assert data["outcome"] == "defects"
    assert "(external review, defects found)" in res.stdout


@pytest.mark.parametrize(
    "extra",
    [
        (),
        ("--clean",),
        ("--defects",),
        ("--source", "external", "--clean"),
        ("--source", "external", "--defects"),
    ],
    ids=["internal", "internal-clean", "internal-defects", "external-clean", "external-defects"],
)
def test_mark_output_never_mentions_rounds(repo, home, extra):
    # The local round counter is gone: no successful `mark` prints a round number,
    # a streak, or a REMINDER, on either stream — even on a repeated defect-bearing
    # mark, which is where the old round-2 reminder fired.
    for i in range(2):
        _stage(repo, f"a = {i + 10}\n")
        res = _mark_raw(repo, home, *extra)
        assert res.returncode == 0, res.stdout + res.stderr
        out = res.stdout + res.stderr
        assert "REMINDER" not in out
        assert "streak" not in out.lower()
        assert "round" not in out.lower()
