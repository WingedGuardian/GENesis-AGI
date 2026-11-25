"""The identity resolver (#2862): one place decides which file a reader gets."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.cc.reflection_bridge._bridge import CCReflectionBridge
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.identity.loader import IdentityLoader
from genesis.identity.paths import POLICY, REPLACE_MARKER, read_identity, sources

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def ident(tmp_path: Path) -> Path:
    d = tmp_path / "identity"
    d.mkdir()
    return d


def test_seed_prefers_the_install_copy_then_the_example(ident):
    assert read_identity("SOUL.md", ident) is None
    (ident / "SOUL.md.example").write_text("shipped soul\n")
    assert read_identity("SOUL.md", ident) == "shipped soul\n"
    (ident / "SOUL.md").write_text("my soul\n")
    assert read_identity("SOUL.md", ident) == "my soul\n"
    assert sources("SOUL.md", ident) == [ident / "SOUL.md"]


def test_plain_never_falls_back_to_an_example(ident):
    (ident / "USER.md.example").write_text("template\n")
    assert read_identity("USER.md", ident) is None


def test_layer_without_a_local_file_is_the_default_verbatim(ident):
    (ident / "STEERING.md").write_text("# Steering\n\n---\n\nrule one\n")
    assert read_identity("STEERING.md", ident) == "# Steering\n\n---\n\nrule one\n"


def test_layer_appends_the_local_file_as_a_new_block(ident):
    (ident / "STEERING.md").write_text("# Steering\n\n---\n\nrule one\n")
    (ident / "STEERING.local.md").write_text("\nrule two\n")
    assert (
        read_identity("STEERING.md", ident) == "# Steering\n\n---\n\nrule one\n\n---\n\nrule two\n"
    )
    assert sources("STEERING.md", ident) == [ident / "STEERING.md", ident / "STEERING.local.md"]


def test_layer_replace_marker_drops_the_default(ident):
    (ident / "CONVERSATION.md").write_text("shipped\n")
    (ident / "CONVERSATION.local.md").write_text(f"\ufeff{REPLACE_MARKER}\nmine only\n")
    assert read_identity("CONVERSATION.md", ident) == (
        "_[The shipped CONVERSATION.md is replaced by CONVERSATION.local.md.]_\n\nmine only\n"
    )


def test_layer_blank_local_file_changes_nothing(ident):
    (ident / "CONVERSATION.md").write_text("shipped\n")
    (ident / "CONVERSATION.local.md").write_text("\n  \n")
    assert read_identity("CONVERSATION.md", ident) == "shipped\n"


def test_layer_local_file_alone(ident):
    (ident / "STEERING.local.md").write_text("rule\n")
    assert read_identity("STEERING.md", ident) == "rule\n"
    (ident / "STEERING.local.md").write_text(f"{REPLACE_MARKER}\nrule\n")
    assert read_identity("STEERING.md", ident) == "rule\n"


def test_read_errors_propagate(ident):
    (ident / "VOICE.md").write_bytes(b"\xff\xfe\xfa")
    with pytest.raises(UnicodeDecodeError):
        read_identity("VOICE.md", ident)


def test_loader_and_assembler_read_through_the_resolver(ident):
    (ident / "SOUL.md.example").write_text("shipped soul")
    (ident / "STEERING.md").write_text("# S\n\n---\n\nshipped rule")
    (ident / "STEERING.local.md").write_text("local rule")
    loader = IdentityLoader(ident)
    assert loader.soul() == "shipped soul"
    assert loader.steering_rules() == ["shipped rule", "local rule"]
    prompt = SystemPromptAssembler(identity_dir=ident).assemble_static()
    assert "shipped soul" in prompt and "local rule" in prompt


def test_reflection_bridge_reads_through_the_resolver(ident):
    (ident / "SOUL.md.example").write_text("shipped soul")
    (ident / "STEERING.local.md").write_text("local rule")
    block = CCReflectionBridge._load_identity_block(SimpleNamespace(_prompt_dir=ident))
    assert block == "shipped soul\n\nlocal rule"


def test_steering_writer_appends_to_the_local_layer(ident):
    (ident / "STEERING.md").write_text("# S\n\n---\n\nshipped rule\n")
    (ident / "STEERING.local.md").write_text("local rule\n")
    loader = IdentityLoader(ident)
    assert "local rule" in loader.steering()  # cache warmed with the layered text
    loader.add_steering_rule("new rule")
    assert (ident / "STEERING.md").read_text() == "# S\n\n---\n\nshipped rule\n"
    assert loader.steering_rules() == ["shipped rule", "local rule", "new rule"]


def _names_a_file(node: ast.AST, names: set[str]) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(node.value == n or node.value.endswith("/" + n) for n in names)
    )


def _path_builds(tree: ast.AST, names: set[str], skip: set[str]) -> list[int]:
    """Lines that build a path to one of *names*: `p / "X"`, `Path("…/X")`,
    `p.joinpath("X")`, `open("…/X")`, or an f-string ending in `/X`.

    Nodes inside a function named in *skip* are ignored.
    """
    skipped: set[int] = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in skip:
            skipped |= {id(n) for n in ast.walk(fn)}
    hits = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            found = _names_a_file(node.right, names)
        elif isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            found = callee in {"Path", "joinpath", "open"} and any(_names_a_file(a, names) for a in node.args)
        elif isinstance(node, ast.JoinedStr):
            found = bool(node.values) and _names_a_file(node.values[-1], names)
        else:
            found = False
        if found:
            hits.append(node.lineno)
    return hits


def test_no_reader_builds_a_path_to_a_resolved_file_itself():
    """Every SEED/LAYER file is read through paths.py, so its policy holds everywhere.

    Scope: a path built from a literal name (see `_path_builds`). A path built
    from a variable (`identity_dir / name`) is invisible here; the readers that
    do that (the hook, the assembler, the bridge, the loader) have behaviour
    tests above and in test_context_injection_budget.py. The dashboard editor
    is the known exception until #2862 moves it.
    """
    names = set(POLICY)
    skip_files = {
        _ROOT / "src/genesis/identity/paths.py",
        _ROOT / "src/genesis/dashboard/routes/config.py",
    }
    skip_functions: dict[Path, set[str]] = {}
    offenders = []
    for base in ("src", "scripts"):
        for py in (_ROOT / base).rglob("*.py"):
            if py in skip_files:
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            lines = _path_builds(tree, names, skip_functions.get(py, set()))
            offenders += [f"{py.relative_to(_ROOT)}:{n}" for n in lines]
    assert offenders == []


def test_a_directory_in_a_files_place_is_unreadable_not_absent(ident):
    (ident / "STEERING.md").mkdir()
    with pytest.raises(IsADirectoryError):
        read_identity("STEERING.md", ident)


def test_a_bom_only_local_file_is_blank(ident):
    (ident / "STEERING.md").write_text("shipped\n")
    (ident / "STEERING.local.md").write_text("﻿\n")
    assert read_identity("STEERING.md", ident) == "shipped\n"
