"""doc_paths.is_doc_path — direct behaviour, plus PARITY with its source of truth.

``is_doc_path`` is a DELIBERATE duplicate of ``git_push_guard._is_doc_path``
(the ``src/`` may-not-import-``scripts/`` boundary; see the module docstring).
Duplicate + parity test is the sanctioned pattern (``_charter_md`` precedent),
and THIS file is the parity half: it loads the hook by path and asserts the two
classifiers agree — over their constant sets, over a hand-picked corpus of the
cases that matter, and over a generated cross-product wide enough that a logic
drift cannot hide between hand-picked examples. Drift in either copy fails here
immediately; fix by editing BOTH copies together.
"""

from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

from genesis.session_awareness import doc_paths

_REPO = Path(__file__).resolve().parents[2]
_GUARD = _REPO / "scripts" / "hooks" / "git_push_guard.py"


@pytest.fixture(scope="module")
def guard_mod():
    """The hook, loaded by path (the tests/test_hooks idiom). Registered in
    sys.modules BEFORE exec — its dataclasses resolve their module from there,
    and an unregistered exec fails on every run (measured, Kimi P2 2026-09-06)."""
    spec = importlib.util.spec_from_file_location("_gpg_for_doc_parity", _GUARD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_gpg_for_doc_parity"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("_gpg_for_doc_parity", None)
        raise
    return mod


# The cases that MATTER, both directions — each one is a decision the
# verification lane's auto-close rides on.
DOCS = [
    "README.md",
    "docs/architecture/CURRENT.md",
    "CLAUDE.md",  # prose at top level — the owner's prompt-exemption intent
    ".claude/skills/genesis-development/SKILL.md",  # prompt surface = prose HERE
    "CHANGELOG.md",
    "LICENSE",  # doc stem, no extension
    "LICENSE.txt",  # .txt admitted only on a doc stem
    "vendor/dep/README.rst",
    "notes.markdown",
    "docs/guide.adoc",
    "a/b/c/contributing.md",
]
CODE = [
    "src/genesis/memory/graph.py",
    "scripts/hooks/git_push_guard.py",
    "config/repo_pulse.yaml",
    "docs/conf.py",  # source under docs/ is still code
    "docs/requirements.txt",  # .txt off a doc stem = manifest, not prose
    "NOTES.txt",  # non-doc stem + .txt
    ".github/workflows/ci.yml",
    "Makefile",
    "",  # empty path fails closed
    "docs/evil\x00.md",  # control char fails closed
    "docs/evil\x85.md",  # NEL — gh --jq emits it literally
    "docs/evil\x7f.md",  # DEL
]


def test_docs_classify_as_prose():
    for path in DOCS:
        assert doc_paths.is_doc_path(path), path


def test_code_classifies_as_code():
    for path in CODE:
        assert not doc_paths.is_doc_path(path), path


def test_constant_sets_match_the_hook(guard_mod):
    """The cheapest drift detector: the sets themselves. A stem or extension
    added to one copy and not the other fails here even for inputs no corpus
    case exercises."""
    assert doc_paths._DOC_EXTS == guard_mod._DOC_EXTS
    assert doc_paths._DOC_STEM_EXTS == guard_mod._DOC_STEM_EXTS
    assert doc_paths._DOC_STEMS == guard_mod._DOC_STEMS


def test_behaviour_parity_over_the_corpus(guard_mod):
    for path in DOCS + CODE:
        assert doc_paths.is_doc_path(path) == guard_mod._is_doc_path(path), path


def test_behaviour_parity_over_a_generated_cross_product(guard_mod):
    """Constants can match while the LOGIC drifts (a changed rule order, a
    dropped lowercasing). Generate paths from the axes that drive the decision
    — stem × extension × depth × case — and require agreement on every cell.
    ~700 cells; a hand corpus is a sample, this is the model's coverage."""
    stems = ["readme", "README", "notice", "notes", "conf", "SKILL", "x"]
    exts = ["md", "MD", "markdown", "rst", "adoc", "txt", "py", "yaml", ""]
    dirs = ["", "docs/", "src/deep/nest/", ".claude/skills/s/"]
    checked = 0
    for d, stem, ext in itertools.product(dirs, stems, exts):
        path = f"{d}{stem}.{ext}" if ext else f"{d}{stem}"
        assert doc_paths.is_doc_path(path) == guard_mod._is_doc_path(path), path
        checked += 1
    assert checked >= 250, "the cross-product shrank — the parity got weaker"
