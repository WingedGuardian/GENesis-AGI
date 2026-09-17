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


def test_destructive_rules_live_where_they_bind():
    """VERIFY-RED: move `deletion` or `non_fast_forward` back into the
    approvals ruleset and this fails.

    The test that decides which ruleset a rule belongs in is: does it need
    APPROVAL semantics? Only `pull_request` does. Nobody approves deleting
    `main` or force-pushing over its history, so leaving those in the bypassed
    set made them decoration for the actor most likely to administer the branch
    — the same defect as a bypassed required check, one rule over (Codex P1,
    PR #1907).
    """
    approvals = json.loads((_RULESET_DIR / "approvals.json").read_text())
    checks = json.loads((_RULESET_DIR / "checks.json").read_text())
    destructive = {"deletion", "non_fast_forward"}
    assert destructive <= {r["type"] for r in checks["rules"]}, (
        "destructive-history rules must sit in the ruleset with NO bypass"
    )
    assert not (destructive & {r["type"] for r in approvals["rules"]})


def test_only_the_approval_rule_needs_the_bypassed_ruleset():
    """The converse, so the split cannot quietly grow. Anything beyond the
    known-bypassed set appearing here is a decision, not a tidy-up."""
    approvals = json.loads((_RULESET_DIR / "approvals.json").read_text())
    assert {r["type"] for r in approvals["rules"]} == {"pull_request", "update", "creation"}


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


def test_an_approval_does_not_survive_the_push_it_approved():
    """A stale approval means the merging code is not the reviewed code.

    The window is contributor-shaped and invisible: approve at commit A, the
    contributor pushes B, and GitHub still counts the approval of A toward the
    one-review requirement. `require_last_push_approval` is what makes the
    approval refer to the head that will actually merge.

    Pinned rather than left to the JSON because flipping it back reads as a
    convenience fix the day someone hits the prompt — and it costs the sole
    maintainer nothing, since self-authored PRs merge through the bypass, not
    through this rule (Codex P1, PR #1907).
    """
    approvals = json.loads((_RULESET_DIR / "approvals.json").read_text())
    pr_rule = next(r for r in approvals["rules"] if r["type"] == "pull_request")
    params = pr_rule["parameters"]
    assert params["require_last_push_approval"] is True, (
        "an approval must not survive the push that changed what it approved"
    )
    assert params["required_approving_review_count"] >= 1, (
        "last-push approval is inert without a review requirement to attach to"
    )


def test_two_live_rulesets_with_one_name_raise_rather_than_overwrite(mod, monkeypatch):
    """The live side must fail as loudly as the local side already does.

    Keyed by name, a duplicate silently kept the last one: a dry run would then
    report the survivor as in sync while the hidden duplicate went on enforcing
    unreviewed rules, and the unmanaged-ruleset report reads the same dict, so
    it loses the duplicate too.

    VERIFY-RED: with the raise removed this returns one entry and asserts
    nothing — which is precisely the silence being fixed, so the test has to
    assert the exception, not the result.
    """

    def fake(args):
        url = args[-1]
        if "/rulesets?" in url or url.endswith("/rulesets"):
            return [
                {"id": 7, "name": "Genesis Main Ruleset", "target": "branch"},
                {"id": 9, "name": "Genesis Main Ruleset", "target": "branch"},
            ]
        rid = int(url.rsplit("/", 1)[-1])
        return {
            "id": rid,
            "name": "Genesis Main Ruleset",
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": {},
            "rules": [{"type": "update"}],
        }

    monkeypatch.setattr(mod, "_gh_json", fake)
    with pytest.raises(RuntimeError) as excinfo:
        mod._live_definitions("owner/name")
    message = str(excinfo.value)
    assert "Genesis Main Ruleset" in message, "the message must name the collision"
    assert "7" in message and "9" in message, (
        "both ids must be reported — the operator has to find the duplicate to delete it"
    )


def test_the_two_rulesets_do_not_both_carry_the_same_rule_type():
    """One rule, one enforcer — but NOT for the reason an earlier draft gave.

    That draft said the binding copy would depend on evaluation order. There is
    no evaluation order: rulesets targeting the same ref AGGREGATE, with no
    priority between them, and where the same rule appears twice the MOST
    RESTRICTIVE version applies. So a duplicated rule does not resolve
    arbitrarily — it resolves strictly, which is a different hazard and a
    smaller one.

    The invariant is kept because the strictest-wins outcome is exactly what the
    split exists to avoid for `pull_request`: a no-bypass copy would bind the
    maintainer too. That makes duplicating a rule a DESIGN decision, not a
    tidy-up — see the README's note on closing the bypass residual, which weighs
    precisely that trade.
    """
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
        if "/rulesets?" in url or url.endswith("/rulesets"):
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
    assert len(calls) == 2, f"expected a listing then a by-id fetch, got {calls}"
    assert calls[0].startswith("repos/owner/name/rulesets"), calls[0]
    assert "includes_parents=false" in calls[0], (
        "the listing must exclude inherited org rulesets"
    )
    assert calls[1] == "repos/owner/name/rulesets/7", (
        "each ruleset must be re-fetched by id, not read from the list view"
    )
    assert live["Genesis Required Checks"]["rules"], "the full read must carry `rules`"


def test_a_no_bypass_ruleset_is_applied_before_a_bypassed_one(mod):
    """Protection is added before anything that can remove it.

    VERIFY-RED: sort by name instead and this fails, because "Genesis Main
    Ruleset" precedes "Genesis Required Checks" alphabetically — which is
    exactly the order that left required checks enforced NOWHERE when a PUT
    failed between the two (Codex P1, PR #1907).
    """
    # The SHIPPED key, not a re-declared copy. The previous version of this
    # test built its own lambda, so inverting or deleting the production sort
    # left the whole suite green — a test grading a private copy of the code,
    # on the very fix that closed a P1 (audit, PR #1907).
    local = mod._local_definitions()
    names = [name for name, _ in sorted(local.items(), key=mod._protection_first)]
    assert names[0] == "Genesis Required Checks", (
        f"the no-bypass ruleset must be reconciled first; got {names}"
    )


def test_server_added_nulls_are_not_drift(mod):
    """GitHub echoes optional fields it was not given (`integration_id: null`).
    Comparing those against a local file that omits the key would make a
    freshly-applied ruleset read as drifted forever."""
    local = {"rules": [{"type": "required_status_checks", "parameters": {"a": 1}}]}
    live = {"rules": [{"type": "required_status_checks", "parameters": {"a": 1, "b": None}}]}
    assert mod._differences(local, live) == []


def test_a_real_value_still_differs_from_an_absent_one(mod):
    """The negative control for the rule above: dropping NULLs must not also
    drop a key whose value is real, or the comparison stops detecting drift."""
    local = {"rules": [{"type": "x"}]}
    live = {"rules": [{"type": "x", "parameters": {"b": 5}}]}
    assert "rules" in mod._differences(local, live)


def test_paginated_pages_are_flattened_not_concatenated(mod, monkeypatch):
    """`--paginate` emits each page as its OWN top-level JSON value.

    VERIFY-RED: drop `--slurp` and feed two adjacent arrays to `json.loads` and
    it raises — which is what the pagination fix would have done at exactly the
    scale it was added for, taking both dry-run and apply to exit 2 without
    reconciling anything (Codex P2, PR #1907).
    """

    class _Ok:
        returncode = 0
        stderr = ""
        stdout = '[[{"id": 1}, {"id": 2}], [{"id": 3}]]'  # --slurp shape

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Ok())
    rows = mod._gh_json(["api", "--paginate", "--slurp", "repos/o/n/rulesets"])
    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}], "pages must be flattened"


def test_a_single_unslurped_call_is_untouched(mod, monkeypatch):
    """The negative control: the flattening applies ONLY to a slurped
    paginated call, so an ordinary single-object read is unaffected."""

    class _Ok:
        returncode = 0
        stderr = ""
        stdout = '{"nameWithOwner": "o/n"}'

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Ok())
    assert mod._gh_json(["repo", "view", "--json", "nameWithOwner"]) == {"nameWithOwner": "o/n"}


def test_creations_are_applied_before_updates(mod, monkeypatch):
    """The ORDER itself, graded end-to-end through `main()`.

    The sort key having the right shape is not the property that matters; what
    matters is that a create lands before an update that can remove protection.
    Nothing exercised `main()`'s apply path at all before this — the dry-run
    test makes both writers RAISE, which proves read-only-ness and nothing
    about sequence.
    """
    seq: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_post", lambda repo, d: seq.append(("POST", d["name"])))
    monkeypatch.setattr(mod, "_put", lambda repo, i, d: seq.append(("PUT", d["name"])))
    monkeypatch.setattr(mod, "_resolve_repo", lambda explicit: "owner/name")
    monkeypatch.setattr(
        mod,
        "_live_definitions",
        lambda repo: {"Genesis Main Ruleset": {"id": 1, "rules": [], "bypass_actors": []}},
    )
    monkeypatch.setattr(sys, "argv", ["apply_rulesets.py", "--apply"])
    assert mod.main() == 0
    assert seq == [
        ("POST", "Genesis Required Checks"),
        ("PUT", "Genesis Main Ruleset"),
    ], f"protection must be created before anything is updated; got {seq}"


def test_a_failed_creation_stops_before_any_update_runs(mod, monkeypatch):
    """The property the ordering EXISTS for, which nothing asserted.

    There is no transaction, so order decides what a mid-run failure leaves
    behind — and the sibling test above grades only the happy-path sequence. If
    the create fails and the run continues, the update loop strips `deletion`,
    `non_fast_forward` and `required_status_checks` out of the bypassed ruleset
    while their replacement was never created: the repository ends STRICTLY
    WEAKER than it started, which is the exact outcome the whole design exists
    to prevent.

    VERIFY-RED: delete the `return 2` on the create-failure path and the rest of
    this file stays green. That is what made this a gap rather than a duplicate
    (audit, PR #1907).
    """
    seq: list[tuple[str, str]] = []

    def _boom(repo, d):
        seq.append(("POST", d["name"]))
        raise RuntimeError("github said no")

    monkeypatch.setattr(mod, "_post", _boom)
    monkeypatch.setattr(mod, "_put", lambda repo, i, d: seq.append(("PUT", d["name"])))
    monkeypatch.setattr(mod, "_resolve_repo", lambda explicit: "owner/name")
    monkeypatch.setattr(
        mod,
        "_live_definitions",
        lambda repo: {"Genesis Main Ruleset": {"id": 1, "rules": [], "bypass_actors": []}},
    )
    monkeypatch.setattr(sys, "argv", ["apply_rulesets.py", "--apply"])
    assert mod.main() == 2, "a failed creation must exit 2, never fall through to 0"
    assert seq == [("POST", "Genesis Required Checks")], (
        "no update may run after a creation failed — the update is what REMOVES "
        f"the protections the failed creation was replacing; got {seq}"
    )


def test_a_slurped_non_list_page_raises_rather_than_degrading(mod, monkeypatch):
    """A page that is a dict (an error object) must NOT reach the row loop.

    Returning the container unflattened let `.get("target")` be None on it, so
    the entry was silently skipped, `live` came back empty, every declared
    ruleset read ABSENT, and `--apply` would POST duplicates of rulesets that
    already exist — the fail-soft degrade this function's docstring forbids.
    """

    class _Ok:
        returncode = 0
        stderr = ""
        stdout = '{"message": "Not Found"}'

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Ok())
    with pytest.raises(RuntimeError, match="refusing to guess"):
        mod._gh_json(["api", "--paginate", "--slurp", "repos/o/n/rulesets"])


def test_the_documentation_rule_is_one_clause_over_the_classifier_sets():
    """Grade the label rule against `_is_doc_path`'s OWN sets, structurally.

    This clause has been wrong three times — matching every `.txt`; then as TWO
    `all-globs-to-all-files` clauses, which each demand the WHOLE diff satisfy
    them, so a PR touching `docs/x.md` AND `LICENSE` matched neither and went
    unlabelled. Every wrong version was reasoned about rather than checked.

    Deliberately NOT a glob-matching test: evaluating minimatch semantics in
    Python means reimplementing a matcher, which is the hand-rolled-parser trap
    this repo has paid for elsewhere. Instead this asserts the two things that
    were actually wrong — the CLAUSE COUNT (structure) and the EXTENSION/STEM
    SETS (content) — read from the classifier rather than restated here.

    Residual, stated: this cannot prove the glob's runtime semantics, only that
    it is one clause carrying the right vocabulary. The action's own run on this
    PR is the end-to-end check.
    """
    import re

    import yaml

    config = yaml.safe_load((_REPO / ".github" / "labeler.yml").read_text())
    clauses = config["documentation"]
    assert len(clauses) == 1, (
        "ONE clause: `all-globs-to-all-files` demands every glob match every "
        "changed file, so two clauses are an OR of whole-diff tests, not a union "
        "— a mixed docs-only PR then matches neither"
    )
    (patterns,) = clauses[0]["changed-files"]
    (glob,) = patterns["all-globs-to-all-files"]

    guard = (_REPO / "scripts" / "hooks" / "git_push_guard.py").read_text()
    doc_exts = set(
        re.search(r"_DOC_EXTS = \{([^}]*)\}", guard).group(1).replace('"', "").split(", ")
    )
    doc_stems = {
        m.strip().strip('",')
        for m in re.search(r"_DOC_STEMS = \{(.*?)\}", guard, re.S).group(1).split("\n")
        if m.strip().strip('",')
    }

    lowered = glob.lower()
    missing_exts = [e for e in doc_exts if f".{e}" not in lowered and e not in lowered]
    assert not missing_exts, f"prose extensions the classifier accepts are unmatched: {missing_exts}"
    missing_stems = [st for st in doc_stems if st not in lowered]
    assert not missing_stems, f"documentation stems the classifier accepts are unmatched: {missing_stems}"
