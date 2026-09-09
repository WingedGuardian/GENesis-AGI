"""The ruleset reconciler, and the invariants that make it safe to run.

This script decides whether the rules protecting `main` are where we believe
they are. The 2026-08-27 audit measured the failure it exists to prevent: a
ruleset was believed to be binding while a single bypass entry made every rule
in it decoration for the merging actor, and nothing said so. So the properties
under test here are mostly about what the script REFUSES to do — degrade to a
reassuring answer, compare against a partial view, or delete something it does
not recognise.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "apply_rulesets.py"
_RULESET_DIR = _REPO / ".github" / "rulesets"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("apply_rulesets", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("apply_rulesets", module)
    spec.loader.exec_module(module)
    return module


# ── The shipped definitions ──────────────────────────────────────────────────
# These assert the OWNER'S RULING (2026-09-09), not a preference: the split
# exists so that required checks bind a merge the approval rule cannot. A later
# edit that adds a bypass actor to the checks ruleset would restore the exact
# condition the split was made to remove, and would do it silently — a bypassed
# rule looks identical to an enforced one in the settings list.


def test_the_checks_ruleset_has_no_bypass_actors():
    checks = json.loads((_RULESET_DIR / "checks.json").read_text())
    assert checks["bypass_actors"] == [], (
        "the checks ruleset must bind EVERY merge including --admin — a bypass "
        "actor here makes the required checks decoration, which is the defect "
        "the two-ruleset split exists to fix"
    )


def test_the_checks_ruleset_requires_exactly_the_agreed_contexts():
    checks = json.loads((_RULESET_DIR / "checks.json").read_text())
    (rule,) = [r for r in checks["rules"] if r["type"] == "required_status_checks"]
    contexts = {c["context"] for c in rule["parameters"]["required_status_checks"]}
    assert contexts == {"test", "leak-detector", "lint"}, (
        "adding a required context makes a red result block every merge until a "
        "human disables the ruleset by hand — that is a decision, not a tweak"
    )


def test_the_approvals_ruleset_keeps_its_bypass():
    """The asymmetry is the design, so it is pinned from both sides.

    Without this, 'remove the bypass' reads as an unambiguous improvement and
    would make every self-authored PR unmergeable: the sole maintainer cannot
    approve their own pull request.
    """
    approvals = json.loads((_RULESET_DIR / "approvals.json").read_text())
    assert approvals["bypass_actors"], (
        "the approvals ruleset must keep the admin bypass — the pull-request "
        "rule is unsatisfiable for a self-authored PR without it"
    )


def test_the_two_rulesets_do_not_both_carry_the_same_rule_type():
    """One rule, one enforcer. A rule type present in both rulesets would be
    enforced under two different bypass postures, and which one bound would
    depend on evaluation order nobody controls."""
    a = {r["type"] for r in json.loads((_RULESET_DIR / "approvals.json").read_text())["rules"]}
    b = {r["type"] for r in json.loads((_RULESET_DIR / "checks.json").read_text())["rules"]}
    assert not (a & b), f"rule types declared in both rulesets: {sorted(a & b)}"


def test_every_definition_is_loadable_and_uniquely_named(mod):
    local = mod._local_definitions()
    assert set(local) == {"Genesis Main Ruleset", "Genesis Required Checks"}


# ── Comparison semantics ─────────────────────────────────────────────────────


def test_reordering_is_not_drift(mod):
    """GitHub does not promise list order. A script that cried wolf on a
    reordering would be ignored by the time it reported something real."""
    local = {"rules": [{"type": "a"}, {"type": "b"}], "conditions": {}}
    live = {"rules": [{"type": "b"}, {"type": "a"}], "conditions": {}}
    assert mod._differences(local, live) == []


def test_a_removed_rule_is_drift(mod):
    local = {"rules": [{"type": "a"}, {"type": "b"}]}
    live = {"rules": [{"type": "a"}]}
    assert "rules" in mod._differences(local, live)


def test_an_added_bypass_actor_is_drift(mod):
    """The single most important thing this script must notice."""
    local = {"bypass_actors": []}
    live = {"bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole"}]}
    assert "bypass_actors" in mod._differences(local, live)


def test_server_managed_fields_are_not_compared(mod):
    """`id`, `created_at` and friends exist only on the live side; comparing
    them would report drift on every run and train the reader to ignore it."""
    local = {"rules": []}
    live = {"rules": [], "id": 42, "created_at": "2026-01-01", "_links": {}}
    assert mod._differences(local, live) == []


# ── Fail directions ──────────────────────────────────────────────────────────


def test_an_unreadable_gh_call_raises_rather_than_returning_empty(mod, monkeypatch):
    """VERIFY-RED anchor: if `_gh_json` swallowed a failure and returned None,
    `_live_definitions` would report an empty repository — i.e. 'every declared
    ruleset is absent', the most dangerous wrong answer this script can give,
    because --apply would then try to create rulesets that already exist."""

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "gh: not authenticated"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Fail())
    with pytest.raises(RuntimeError, match="failed"):
        mod._gh_json(["api", "repos/x/y/rulesets"])


def test_an_unmanaged_live_ruleset_is_never_deleted():
    """There is no delete path at all — asserted structurally, because a
    reviewer cannot prove absence by reading a long file.

    Over the AST's string CONSTANTS, not the file text: a substring search
    matches the module docstring's own "DELETES NOTHING" and fails on prose
    that says the right thing, which is a classifier graded on the case it
    was never going to see.
    """
    import ast

    tree = ast.parse(_SCRIPT.read_text())
    # Docstrings are each body's first statement. Collected by NODE IDENTITY,
    # not by value: `ast.get_docstring` returns a CLEANED string (dedented and
    # stripped) that never equals the raw `ast.Constant.value`, so a
    # value-based filter silently excludes nothing — which is how the first
    # version of this test failed on the module docstring's own
    # "DELETES NOTHING".
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))
    code_strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]
    offenders = [s for s in code_strings if "DELETE" in s.upper()]
    assert not offenders, f"this script must never delete a ruleset; found {offenders}"


def test_dry_run_writes_nothing(mod, monkeypatch, capsys):
    """The dry run is what a person reads before allowing a write, so it must
    be provably read-only: any state-changing call fails the test."""
    monkeypatch.setattr(mod, "_resolve_repo", lambda explicit: "owner/name")
    monkeypatch.setattr(
        mod,
        "_live_definitions",
        lambda repo: {"Genesis Main Ruleset": {"id": 1, "rules": [], "bypass_actors": []}},
    )

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a --dry-run must not write")

    monkeypatch.setattr(mod, "_post", _boom)
    monkeypatch.setattr(mod, "_put", _boom)
    monkeypatch.setattr(sys, "argv", ["apply_rulesets.py", "--dry-run"])

    assert mod.main() == 1  # drift present -> exit 1, not 0
    out = capsys.readouterr().out
    assert "DRIFT" in out or "ABSENT" in out


def test_an_error_exits_two_not_zero(mod, monkeypatch, capsys):
    """Exit 0 means 'in sync'. An error must never be able to claim it."""

    def _raise(explicit):  # noqa: ANN001
        raise RuntimeError("no auth")

    monkeypatch.setattr(mod, "_resolve_repo", _raise)
    monkeypatch.setattr(sys, "argv", ["apply_rulesets.py", "--dry-run"])
    assert mod.main() == 2


# ── The mirrors are a CHOKEPOINT, not a convention ───────────────────────────
# .github/CODEOWNERS and .github/labeler.yml both restate the enforcement-hook
# surface, because GitHub cannot read a Python constant. A restatement drifts:
# the first version of both carried 6 of the 27 named files, so ~20 wired hooks
# (pretool_check.py, review_enforcement_commit.py, …) matched no glob and a PR
# editing one got no `gate-surface` label at all — the exact annotation the
# file exists to provide. These diff the mirrors against the authority, so the
# next hook cannot be added to the frozenset and silently miss both files.


def _authoritative_surface() -> tuple[list[str], list[str]]:
    """`_HOOK_SURFACE_PREFIXES` / `_HOOK_SURFACE_FILES`, read from the source.

    Parsed rather than imported: importing the guard mutates `sys.path` at
    module scope and pulls in four sibling script modules, which is a large
    side effect for two literals.
    """
    import ast

    tree = ast.parse((_REPO / "scripts" / "hooks" / "git_push_guard.py").read_text())
    prefixes: tuple[str, ...] = ()
    files: frozenset[str] = frozenset()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "_HOOK_SURFACE_PREFIXES":
                prefixes = ast.literal_eval(node.value)
            elif target.id == "_HOOK_SURFACE_FILES":
                inner = node.value.args[0] if isinstance(node.value, ast.Call) else node.value
                files = ast.literal_eval(inner)
    assert prefixes and files, "could not read the hook-surface constants"
    return list(prefixes), sorted(files)


def test_the_labeler_globs_cover_every_hook_surface_path():
    import yaml

    prefixes, files = _authoritative_surface()
    config = yaml.safe_load((_REPO / ".github" / "labeler.yml").read_text())
    globs = set()
    for clause in config["gate-surface"]:
        for patterns in clause["changed-files"]:
            globs.update(patterns["any-glob-to-any-file"])
    missing = [f for f in files if f not in globs]
    missing += [p for p in prefixes if f"{p}**" not in globs]
    assert not missing, (
        "these hook-surface paths get no gate-surface label: " + ", ".join(missing)
    )


def test_codeowners_covers_every_hook_surface_path():
    prefixes, files = _authoritative_surface()
    owned = {
        line.split()[0].lstrip("/")
        for line in (_REPO / ".github" / "CODEOWNERS").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = [f for f in files if f not in owned]
    missing += [p for p in prefixes if p not in owned]
    assert not missing, "these hook-surface paths have no CODEOWNERS entry: " + ", ".join(missing)


def test_the_live_read_fetches_each_ruleset_in_full(mod, monkeypatch):
    """The list endpoint omits `rules` and `bypass_actors` — the two fields
    that decide whether a ruleset does anything — so `_live_definitions`
    re-fetches each by id.

    VERIFY-RED: this is the safety property the function's docstring claims,
    and every other test in this file monkeypatches `_live_definitions`
    wholesale, so reverting it to a list-view comparison kept all of them
    green. Stubbing at the `_gh_json` boundary instead is what makes the
    claim testable: the list row below carries NO rules, and a comparison
    against it would report a gutted ruleset as in sync.
    """
    calls: list[str] = []

    def fake(args):
        url = args[-1]
        calls.append(url)
        if url.endswith("/rulesets"):
            return [{"id": 7, "name": "Genesis Required Checks", "target": "branch"}]
        return {
            "id": 7,
            "name": "Genesis Required Checks",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {},
            "rules": [{"type": "required_status_checks"}],
        }

    monkeypatch.setattr(mod, "_gh_json", fake)
    live = mod._live_definitions("owner/name")
    assert calls == ["repos/owner/name/rulesets", "repos/owner/name/rulesets/7"], (
        "each ruleset must be re-fetched by id, not read from the list view"
    )
    assert live["Genesis Required Checks"]["rules"], "the full read must carry `rules`"
