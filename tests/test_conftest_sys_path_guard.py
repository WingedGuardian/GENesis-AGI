"""Regression backstop for the conftest.py sys.path guard.

The guard at the top of ``tests/conftest.py`` inserts the current
worktree's ``src`` directory at ``sys.path`` position 0 before any
``genesis.*`` import runs. Without it, tests from a sibling worktree
silently resolve ``genesis.*`` from main's source tree (via the venv's
editable-install ``.pth`` file), producing tests that report PASS/FAIL
against the wrong code.

The failure mode is silent. If someone accidentally deletes, comments
out, or breaks the guard, nothing shouts — sibling-worktree runs just
go back to testing main instead of the branch under test. This file
is the loud alarm: it asserts that ``genesis`` (and a representative
submodule) resolves inside ``_WORKTREE_SRC``. If the guard is removed,
this test fails immediately on any sibling worktree and passes
harmlessly in main (where the directories are the same).
"""

from __future__ import annotations

from pathlib import Path


def _expected_src() -> Path:
    """The src dir that conftest.py's guard should have prepended."""
    # This must match the computation in conftest.py. If that
    # computation changes, update here too.
    return (Path(__file__).resolve().parent.parent / "src").resolve()


def test_genesis_package_resolves_inside_worktree_src() -> None:
    """The top-level ``genesis`` package resolves inside this worktree."""
    import genesis

    pkg_path = Path(genesis.__file__).resolve()
    expected = _expected_src()
    assert str(pkg_path).startswith(str(expected)), (
        "conftest.py sys.path guard appears broken or removed: "
        f"genesis.__file__={pkg_path} does not live under "
        f"{expected}. If this test fails in a sibling worktree but "
        "passes in main, the guard is no longer effective and "
        "sibling-worktree test runs are resolving genesis.* from "
        "main's src tree."
    )


def test_submodule_resolves_inside_worktree_src() -> None:
    """A representative submodule also resolves inside this worktree.

    Covers the case where the top-level ``genesis`` package resolves
    correctly but a submodule's import chain drops through to a
    different ``genesis`` tree — unusual, but cheap to verify.
    """
    import genesis.mcp.health.manifest as m

    mod_path = Path(m.__file__).resolve()
    expected = _expected_src()
    assert str(mod_path).startswith(str(expected)), (
        "conftest.py sys.path guard is not shadowing submodule "
        f"imports: {m.__name__}.__file__={mod_path} does not live "
        f"under {expected}."
    )
