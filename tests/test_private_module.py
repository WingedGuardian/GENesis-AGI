"""Locks the REASON ``tests.conftest.private_module`` registers before it execs.

This file exists because the requirement it pins was very nearly deleted. An
earlier revision of ``private_module``'s docstring declared the inherited
"``@dataclass`` needs the name registered" rationale FALSE, on a probe that
exec'd a module carrying ``@dataclass`` and ``@dataclass(slots=True)`` but NOT
``from __future__ import annotations``. Without postponed annotations
``dataclasses._process_class`` reaches an empty-globals fallback and raises
nothing; WITH them it takes a path that has no fallback, and
``dataclasses._is_type`` dereferences ``sys.modules.get(cls.__module__)`` —
``None`` for an unregistered module. A prose claim cannot hold that distinction
open, so this is a test instead: delete the registration line in
``private_module`` and it fails.

Scope: CPython 3.12 (what CI pins, 13 jobs). If a future interpreter removes the
requirement, the first test here fails — which is the correct signal to
re-verify before relaxing ``private_module``, not a reason to loosen the test.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import sys
import typing
from pathlib import Path

import pytest

from tests.conftest import private_module

_REPO_ROOT = Path(__file__).resolve().parents[1]

_FIXTURE = """\
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Shape:
    name: str
    count: int
"""


def _write_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "postponed_dc.py"
    path.write_text(_FIXTURE)
    # Guard-the-guard: the fixture is only evidence if it really carries BOTH
    # properties. Asserting them is what stops this becoming the strawman the
    # original probe was — that probe was wrong precisely by omitting the first.
    src = path.read_text()
    assert "from __future__ import annotations" in src
    assert "@dataclass" in src
    return path


def _exec_unregistered(path: Path, name: str):
    """Load `path` WITHOUT registering `name` — the shape private_module avoids."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    assert name not in sys.modules, "fixture name must start unregistered"
    spec.loader.exec_module(mod)
    return mod


def test_exec_without_registering_the_name_fails_on_a_postponed_annotation_dataclass(
    tmp_path: Path,
) -> None:
    """The requirement itself. This is why the registration line is load-bearing."""
    path = _write_fixture(tmp_path)

    with pytest.raises(AttributeError) as excinfo:
        _exec_unregistered(path, "postponed_dc_unregistered")

    # Assert the RIGHT failure, not merely a failure: a RED for the wrong reason
    # is the trap this repo names explicitly. The message names __dict__ because
    # `sys.modules.get(...)` returned None and was dereferenced.
    assert "__dict__" in str(excinfo.value), (
        f"raised AttributeError for an unexpected reason: {excinfo.value!r}. "
        "This test is only meaningful if the failure is the missing sys.modules "
        "entry — re-verify against dataclasses._is_type before changing it."
    )


def test_private_module_satisfies_that_requirement(tmp_path: Path) -> None:
    """The helper handles what raw exec_module cannot — the mutation target.

    Deleting ``sys.modules[name] = mod`` from ``private_module`` fails HERE.
    """
    path = _write_fixture(tmp_path)

    mod = private_module("postponed_dc_via_helper", path)

    assert dataclasses.is_dataclass(mod.Shape)
    assert [f.name for f in dataclasses.fields(mod.Shape)] == ["name", "count"]
    # And it still restores: leaving the name bound is the leak one step removed.
    assert "postponed_dc_via_helper" not in sys.modules


def test_a_self_importing_module_sees_the_private_copy(tmp_path: Path) -> None:
    """The OTHER reason registration must precede exec, per the docstring.

    A module that imports itself by name during exec must resolve to THIS copy,
    not to a previously-registered one. Without the pre-exec registration it
    would either fail to import or bind a stale sibling — so this and the
    dataclass test pin the two independent reasons for the same line.
    """
    path = tmp_path / "selfimp.py"
    path.write_text("import selfimp\n\nMARK = 'private'\nSEEN = selfimp is not None\n")

    # Seed a DECOY under the same name; the private copy must win during exec.
    decoy = tmp_path / "decoy.py"
    decoy.write_text("MARK = 'decoy'\n")
    sys.modules.pop("selfimp", None)
    decoy_mod = private_module("selfimp", decoy)
    assert decoy_mod.MARK == "decoy"

    mod = private_module("selfimp", path)
    assert mod.MARK == "private"
    assert mod.SEEN is True
    assert "selfimp" not in sys.modules


def test_the_fixture_shape_is_not_a_strawman() -> None:
    """Real scripts in this repo carry the shape, so the lock models live code.

    Without this, the fixture could drift into a shape nobody writes and the
    lock above would pin a requirement that no longer applies to anything.
    """
    scripts = _REPO_ROOT / "scripts"
    carriers = [
        p
        for p in sorted(scripts.rglob("*.py"))
        if "from __future__ import annotations" in (src := p.read_text())
        # ANCHORED on purpose: a bare `"@dataclass" in src` also matches the word
        # inside a COMMENT, and scripts/hooks/git_push_guard.py carries such a
        # comment while defining no dataclass at all. The substring form would
        # keep this test green after the last real carrier disappeared, which is
        # the one thing it exists to notice.
        and re.search(r"^@dataclass", src, re.M)
    ]
    assert carriers, (
        "no script under scripts/ carries BOTH postponed annotations and a "
        "@dataclass — the fixture no longer models live code, so re-derive "
        "whether private_module's registration is still load-bearing."
    )


def test_the_limit_after_restore__previously_unbound(tmp_path: Path) -> None:
    """The LIMIT in ``private_module``'s docstring, branch 1 of 2.

    That paragraph has been WRONG TWICE in prose, in both directions, so it is a
    test now. An earlier revision of THIS test enumerated and EXEC'd every real
    carrier under ``scripts/`` — which dragged ``migrate_reference_data``'s
    module-level ``load_dotenv(..., override=True)`` into the test process and
    permanently replaced environment variables for every test after it. A test
    written to prevent cross-test contamination was causing it. Synthetic
    fixtures prove the same mechanism and touch no process global.

    Branch 1: the name was UNBOUND beforehand, so the restore POPS it and the
    annotation resolves against nothing.
    """
    path = tmp_path / "unbound_mod.py"
    path.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "class Check: pass\n"
        "@dataclass\n"
        "class D:\n"
        "    c: Check\n"
    )
    sys.modules.pop("unbound_mod", None)

    mod = private_module("unbound_mod", path)

    assert "unbound_mod" not in sys.modules  # popped, per the restore contract
    assert dataclasses.fields(mod.D)[0].name == "c"  # fields() is unaffected
    with pytest.raises(NameError):
        typing.get_type_hints(mod.D)


def test_the_limit_after_restore__previously_bound(tmp_path: Path) -> None:
    """Branch 2, and it is the DANGEROUS one — it does not raise.

    When the name was already bound, the restore puts the PREVIOUS object back,
    so the name is NOT unbound and ``get_type_hints`` resolves happily — against
    the CANONICAL module's globals. It returns a same-named class from a
    different module object, silently, which is worse than the NameError of
    branch 1 because nothing signals it.

    The docstring said "on return the name is UNBOUND" without qualification.
    That is true only of branch 1.
    """
    body = (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "class Check: pass\n"
        "@dataclass\n"
        "class D:\n"
        "    c: Check\n"
    )
    canon_path = tmp_path / "canon_src.py"
    priv_path = tmp_path / "priv_src.py"
    canon_path.write_text(body)
    priv_path.write_text(body)

    sys.modules.pop("shared_name", None)
    canonical = private_module("shared_name", canon_path)
    sys.modules["shared_name"] = canonical  # now canonically bound
    try:
        private = private_module("shared_name", priv_path)

        assert sys.modules["shared_name"] is canonical  # previous object restored
        hints = typing.get_type_hints(private.D)
        # Resolves — and to the WRONG object. This is the finding.
        assert hints["c"] is canonical.Check
        assert hints["c"] is not private.Check
    finally:
        sys.modules.pop("shared_name", None)
