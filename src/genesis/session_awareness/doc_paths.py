"""Deterministic docs-vs-code path classifier — a PINNED DUPLICATE, not an import.

``is_doc_path`` answers: is this changed-file path provably prose? The
verification lane (issue #1718 half B) auto-closes a merged PR's obligation row
only when EVERY changed path is prose — the owner's deterministic exemption, so
no model is ever asked whether a prompt or docs change "needs an E2E".

THIS IS A DELIBERATE DUPLICATE of ``scripts/hooks/git_push_guard._is_doc_path``,
and the duplication is the repo's documented answer, not an accident to fix:

* ``src/`` must not import from ``scripts/`` (the one-way rule
  ``session_charter.py`` and ``ledger_worker.py`` both record), and
  ``git_push_guard.py`` additionally mutates process-global ``sys.path`` at
  import time — a ~7,000-line module and four sibling script modules dragged
  into a worker process for one 35-line pure function.
* The sanctioned pattern for exactly this boundary is DUPLICATE + PARITY TEST
  (precedent: ``_charter_md``, pinned byte-identical by
  ``tests/test_scripts/test_precompact_charter.py``). Here the parity test is
  ``tests/test_session_awareness/test_doc_paths.py``, which loads the hook by
  path and asserts both classifiers agree over a shared corpus — drift fails CI
  immediately. Edit BOTH copies together, always.

Do NOT "unify" this with ``review_enforcement_commit._is_docs_or_config``: that
sibling answers a DIFFERENT question (does this commit need review at all) and
deliberately disagrees — it calls ``.claude/skills/**/SKILL.md`` code and
``.yaml`` docs, the exact opposite of this classifier on both counts. One
definition per question; two questions.

Fail direction, inherited and load-bearing: this is a fail-CLOSED ALLOWLIST.
A path is prose only when provably so; anything unknown, empty, or
control-char-bearing classifies as code — which here means the obligation row
STAYS OPEN. A misclassification toward "code" costs a validator a look; a
misclassification toward "docs" silently forgives an unverified merge.

Pure, stdlib-only, no I/O.
"""

from __future__ import annotations

# ``.txt`` is deliberately NOT a blanket doc extension: a build/dep manifest
# (requirements.txt, CMakeLists.txt) carries it too — prose only on a known
# doc-named stem (LICENSE.txt, README.txt). A denylist of code extensions was
# rejected in the source classifier's review: it cannot enumerate every
# source/config type, and an allowlist fails closed on the unknown.
_DOC_EXTS = {"md", "markdown", "rst", "adoc"}
_DOC_STEM_EXTS = _DOC_EXTS | {"txt", ""}
_DOC_STEMS = {
    "changelog",
    "readme",
    "license",
    "notice",
    "copying",
    "authors",
    "contributing",
}


def is_doc_path(path: str) -> bool:
    """Whether *path* is provably documentation. Mirror of
    ``git_push_guard._is_doc_path`` — see the module docstring; edit both.

    Fail-closed: a source/config/manifest file (including under ``docs/`` and
    including a ``.txt`` build manifest), a missing/empty path, or a
    control-char-bearing path all classify as code.
    """
    # Reject the COMPLETE control-character range (Unicode Cc): C0 (<0x20), DEL
    # (0x7F), and C1 (0x80-0x9F, incl. NEL 0x85 which ``gh --jq`` emits
    # literally).
    if not path or any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in path):
        return False
    base = path.rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    ext = ext.lower() if dot else ""
    stem_l = (stem if dot else base).lower()
    # (1) A known doc-named file with a doc/text/empty extension, at any depth.
    if stem_l in _DOC_STEMS and ext in _DOC_STEM_EXTS:
        return True
    # (2) An unambiguous documentation extension, at any depth.
    return ext in _DOC_EXTS
