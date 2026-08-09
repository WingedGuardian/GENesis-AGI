"""Tests for the review-DEPTH gate primitives in scripts/.

Two units under test (both fail-open / advisory — the CI review-depth check is the
real backstop):
  * review_scope.classify_change_substantiality — STAGED-diff (--cached) based
    substantiality, so it shares the review marker's basis (no mark-vs-commit
    thrash). A surface-area × risk model: substantial when reviewable lines ≥ 50 OR
    >1 code file (surface area) OR a domain-sensitive scope_tag (auth/api/migrations —
    risk). Mere newness of a file is NOT a trigger. Docs/binary/vendored lines never
    inflate it.
  * review_state._evidence_is_adversarial — lenient STRUCTURAL check (ladder OR
    scope-check) AND file:line engagement AND a length floor, so a real audit in
    any reviewer's vocabulary passes but a shallow "looks good" one-liner fails.

All git fixtures are synthetic tmp_path repos — install-agnostic.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_rs = _load("review_scope")
_state = _load("review_state")


# --------------------------------------------------------------------------- #
# git fixtures
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return out.stdout


def _mk_repo(tmp_path: Path) -> Path:
    """Repo seeded with committed baseline files the tests then stage edits onto."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.md").write_text("seed\n")
    (repo / "helper.py").write_text("def helper():\n    return 1\n")
    (repo / "helper2.py").write_text("def helper2():\n    return 2\n")
    (repo / "auth_service.py").write_text("def login():\n    return True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _classify(repo: Path) -> str:
    return _rs.classify_change_substantiality(cwd=str(repo))


# --------------------------------------------------------------------------- #
# classify_change_substantiality
# --------------------------------------------------------------------------- #
def test_small_single_code_edit_is_inline(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "helper.py").write_text("def helper():\n    return 3\n")  # 1-line change
    _git(repo, "add", "helper.py")
    assert _classify(repo) == "inline"


def test_fifty_plus_reviewable_lines_is_substantial(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "helper.py").write_text(
        "def helper():\n" + "".join(f"    x{i} = {i}\n" for i in range(60))
    )
    _git(repo, "add", "helper.py")
    assert _classify(repo) == "substantial"


def test_more_than_one_code_file_is_substantial(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "helper.py").write_text("def helper():\n    return 3\n")
    (repo / "helper2.py").write_text("def helper2():\n    return 4\n")
    _git(repo, "add", "helper.py", "helper2.py")
    assert _classify(repo) == "substantial"


def test_small_new_code_file_is_inline(tmp_path):
    # Surface-area × risk model: newness ALONE is not substantial. A trivial single new
    # file of little consequence is INLINE — Rule 2 still requires *a* review, just not
    # an adversarial audit. It escalates only via line count, >1 file, or a domain scope
    # (the three tests around this one). (verify-RED: asserted substantial before this.)
    repo = _mk_repo(tmp_path)
    (repo / "brandnew.py").write_text("def n():\n    return 0\n")  # small AND new
    _git(repo, "add", "brandnew.py")
    assert _classify(repo) == "inline"


def test_large_new_code_file_is_substantial(tmp_path):
    # A new file is NOT exempt when it carries real surface area (≥50 reviewable lines) —
    # the line-count trigger fires regardless of add-vs-modify.
    repo = _mk_repo(tmp_path)
    (repo / "brandnew.py").write_text(
        "def n():\n" + "".join(f"    y{i} = {i}\n" for i in range(60))
    )
    _git(repo, "add", "brandnew.py")
    assert _classify(repo) == "substantial"


def test_domain_sensitive_scope_tag_is_substantial(tmp_path):
    repo = _mk_repo(tmp_path)
    # small, single, existing (not new) — the ONLY trigger is the auth scope_tag: a
    # small change to a CRITICAL file still warrants an adversarial audit (risk axis).
    (repo / "auth_service.py").write_text("def login():\n    return False\n")
    _git(repo, "add", "auth_service.py")
    assert _classify(repo) == "substantial"


def test_small_new_domain_file_is_substantial(tmp_path):
    # Risk axis for a NEW file: small + single + new, but auth-scoped → substantial.
    # Newness is not the trigger; the critical scope is.
    repo = _mk_repo(tmp_path)
    (repo / "auth_helper.py").write_text("def check():\n    return False\n")  # small, new, auth
    _git(repo, "add", "auth_helper.py")
    assert _classify(repo) == "substantial"


def test_large_docs_only_change_is_inline(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "seed.md").write_text("seed\n" + "".join(f"line {i}\n" for i in range(80)))
    _git(repo, "add", "seed.md")
    assert _classify(repo) == "inline"  # docs lines must NOT inflate substantiality


def test_docs_flood_plus_tiny_code_is_inline(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "seed.md").write_text("seed\n" + "".join(f"line {i}\n" for i in range(80)))
    (repo / "helper.py").write_text("def helper():\n    return 9\n")  # 1 reviewable line
    _git(repo, "add", "seed.md", "helper.py")
    assert _classify(repo) == "inline"  # only reviewable (non-docs) lines count


def test_rename_single_code_file_is_inline(tmp_path):
    # SHOULD-FIX 1 (audit-found): a pure rename of ONE code file is ONE logical file,
    # not two. name-status emits both src+dst; counting records double-counted it as
    # >1 code file → substantial. (verify-RED: this asserted substantial before the fix.)
    repo = _mk_repo(tmp_path)
    _git(repo, "mv", "helper.py", "renamed_helper.py")
    assert _classify(repo) == "inline"


def test_new_binary_asset_is_inline(tmp_path):
    # NOTE 3 (audit-found): a binary asset is not code to review — adding one must not
    # inflate substantiality (it is excluded from the reviewable set entirely).
    repo = _mk_repo(tmp_path)
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    _git(repo, "add", "logo.png")
    assert _classify(repo) == "inline"


def test_no_staged_changes_is_inline(tmp_path):
    repo = _mk_repo(tmp_path)
    assert _classify(repo) == "inline"


def test_classify_range_substantial(tmp_path):
    # classify_range_substantiality uses base...HEAD (the CI PR-range basis) with the
    # SAME predicate as the staged path.
    repo = _mk_repo(tmp_path)
    (repo / "big.py").write_text("def f():\n" + "".join(f"    y{i} = {i}\n" for i in range(60)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "big")
    base = _git(repo, "rev-parse", "HEAD~1").strip()
    assert _rs.classify_range_substantiality(base, cwd=str(repo)) == "substantial"


def test_outside_git_repo_is_unknown(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _rs.classify_change_substantiality(cwd=str(plain)) == "unknown"


# --------------------------------------------------------------------------- #
# _evidence_is_adversarial
# --------------------------------------------------------------------------- #
_ARCHITECT = (
    "Scope Check: CLEAN\n"
    "BLOCKER 1 — the marker at review_state.py:241 is self-reported.\n"
    "SHOULD-FIX 2 — classify at review_scope.py:433 reuses the wrong basis.\n"
    "NOTE 3 — import topology risk at review_scope.py:49.\n"
    "Completion status: DONE_WITH_CONCERNS.\n" + "x" * 300
)
_SECURITY = (
    "CRITICAL: SQL injection in db.py:42 — unparameterized query.\n"
    "HIGH: path traversal at io.py:88.\n"
    "LOW: verbose error at api.py:12.\n" + "y" * 300
)
_CODEX = "P1: race at loop.py:1784.\nP2: cooldown at loop.py:1790.\n" + "z" * 350
_JSON_AUDIT = '[{"file":"a.py","line":10,"severity":"high","category":"async_state"}]\n' + "q" * 350
_CLEAN_AUDIT = (
    "Scope Check: covered helper.py:2, auth_service.py:2, review_scope.py:433. "
    "Enumerated the edge/boundary/sentinel class; no BLOCKER or SHOULD-FIX found. "
    "NOTE: consider a test for the rename case at review_scope.py:472.\n" + "w" * 250
)


def test_accepts_architect_ladder():
    ok, _ = _state._evidence_is_adversarial(_ARCHITECT)
    assert ok


def test_accepts_security_ladder():
    assert _state._evidence_is_adversarial(_SECURITY)[0]


def test_accepts_codex_ladder():
    assert _state._evidence_is_adversarial(_CODEX)[0]


def test_accepts_code_auditor_json():
    assert _state._evidence_is_adversarial(_JSON_AUDIT)[0]


def test_accepts_clean_but_engaged_audit():
    assert _state._evidence_is_adversarial(_CLEAN_AUDIT)[0]


def test_rejects_short_stub():
    ok, msg = _state._evidence_is_adversarial("Looks good. 88% confident.")
    assert not ok
    assert "short" in msg


def test_rejects_long_prose_without_ladder_or_fileline():
    ok, msg = _state._evidence_is_adversarial("The change looks reasonable overall. " * 30)
    assert not ok


def test_rejects_uppercase_ladder_without_file_engagement():
    # An uppercase ladder label is present, but there is NO file:line / JSON
    # engagement → rejected on engagement (not on the ladder).
    ok, msg = _state._evidence_is_adversarial(
        "CRITICAL: there is a serious problem somewhere in this module. " * 12
    )
    assert not ok
    assert "file:line" in msg


def test_rejects_shallow_high_confidence_prose_even_with_fileline():
    # SHOULD-FIX 2 (audit-found): lowercase "high confidence / low risk" is the exact
    # phrasing a rubber stamp uses — it must NOT satisfy the ladder even WITH a
    # file:line pointer. (This PASSED before the case-sensitivity fix — verify-RED.)
    text = "Reviewed helper.py:2 — high confidence, low risk, medium effort overall. " * 12
    ok, msg = _state._evidence_is_adversarial(text)
    assert not ok
    assert "ladder" in msg


# --------------------------------------------------------------------------- #
# get_marker_depth
# --------------------------------------------------------------------------- #
def test_get_marker_depth_absent_is_none_false(tmp_path, monkeypatch):
    monkeypatch.setattr(_state, "_MARKER_DIR", tmp_path / "markers")
    monkeypatch.setattr(_state, "_LEGACY_STATE_FILE", tmp_path / "legacy.json")
    level, adversarial = _state.get_marker_depth(cwd=str(tmp_path))
    assert level is None
    assert adversarial is False
