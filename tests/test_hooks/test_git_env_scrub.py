"""Ambient git env vars must not reach the review gates' own git calls.

An exported ``GIT_DIR`` / ``GIT_WORK_TREE`` / ``GIT_INDEX_FILE`` redirects git away
from the directory a caller passed explicitly as ``cwd``, so a gate that reads git
for a decision reads a DIFFERENT repository than the one it is deciding about. Four
shared decision inputs were MEASURED corrupt on 2026-09-17, all in the FAIL-OPEN
direction — see the regression test below, which is that measurement written down.

Two layers close it and this file locks both halves that live in Python:

* the **parity** tests, which keep the FOUR hand-written copies of the variable
  list in step (the launcher's bash array cannot import a Python tuple, so the
  duplication is structural and is pinned rather than trusted);
* the **regression** test, which drives the four real inputs through real git in
  scratch repositories under a poisoned environment.

The launcher's own leg — that a hook LAUNCHED by ``.claude/hooks/genesis-hook``
gets a scrubbed environment — is tested beside its sibling in
``tests/test_scripts/test_genesis_hook_wrapper.py``, where the main-tree/worktree
harness already lives.

Hermetic: scratch git repos under ``tmp_path``. None of the four functions writes
anything — they are pure reads — so the real marker store under ``~/.genesis/``
is never touched. That matters: ``review_state._MARKER_DIR`` is hardcoded with no
env override, so a test that WROTE a marker would pollute production state.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _REPO_ROOT / ".claude" / "hooks" / "genesis-hook"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import review_scope  # noqa: E402
import review_state  # noqa: E402

from genesis.session_awareness.zero_drop_git import (  # noqa: E402
    _GIT_ENV_UNSET as ZERO_DROP_UNSET,
)

#: Config FILE sources. NO copy may handle these, in either direction — unsetting
#: loosens, and pinning removes ``safe.directory`` plus the remote-auth settings.
#: The test below carries both measurements.
_CONFIG_FILE_VARS = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")

# ── the four copies must agree ────────────────────────────────────────────────


def _launcher_bash_list() -> tuple[str, ...]:
    """The variable names inside the launcher's ``_GIT_ENV_SCRUB=( … )`` array.

    Parsed rather than hardcoded here: a second hand-written copy in the test
    would drift exactly like the ones it is meant to pin, and would then agree
    with nothing while looking like a check.

    Known false-pass mode, stated because it is not closed here: this reads the
    FIRST array declaration, so a later re-assignment (``_GIT_ENV_SCRUB=()``)
    would pass parity while the runtime array is empty. The behavioural test in
    ``tests/test_scripts/test_genesis_hook_wrapper.py`` is what actually catches
    that — it asserts on the launched child's own environment and carries an
    unscrubbed control. The parser's other failure modes fail LOUDLY (two ``-u``
    pairs on one line yield no matches and trip ``assert names``; a trailing
    comment drops that name and parity fails naming it).
    """
    text = _LAUNCHER.read_text()
    m = re.search(r"^_GIT_ENV_SCRUB=\(\n(.*?)^\)$", text, re.MULTILINE | re.DOTALL)
    assert m, "launcher no longer declares a _GIT_ENV_SCRUB=( … ) array"
    names = re.findall(r"^\s*-u\s+([A-Z_][A-Z0-9_]*)\s*$", m.group(1), re.MULTILINE)
    assert names, "the _GIT_ENV_SCRUB array parsed as empty"
    return tuple(names)


def test_no_copy_anywhere_touches_the_config_FILE_variables():
    """``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` are handled in NEITHER
    direction, in all four copies. Both directions were MEASURED to break
    something, which is why this is asserted by NAME rather than left implicit.

    UNSETTING re-enables ``$HOME/.gitconfig``, so a scrub meant to isolate
    loosens, and a caller that set ``/dev/null`` for isolation loses it.

    PINNING to an empty file removes a CAPABILITY. ``safe.directory`` is readable
    only from protected config, and repo-local config cannot restore it
    (MEASURED: still rc=129), so under a uid mismatch — a bind-mounted
    devcontainer, container CI, a hook under sudo — git REFUSES with empty
    stdout and ``_staged_content_hash``'s empty-output sentinel is ``"clean"``:
    NOTHING STAGED, over real staged work. Pinning also removes
    ``credential.helper`` and ``url.*.insteadOf``, which ``zero_drop_git``'s env
    feeds to ``git ls-remote`` and ``gh``.

    And in the launcher specifically it breaks ``git_push_guard``, which PREDICTS
    what a ``git push`` will do: MEASURED through the real
    ``_push_config_is_simple``, with ``push.default = matching`` in
    ``~/.gitconfig`` it returns False (prompts) when the config is visible and
    True — ALLOWS SILENTLY — once the config is pinned away.

    What closes the one config route measured to move a decision is a command
    FLAG, pinned by ``test_a_global_attributes_file_cannot_downgrade_substantiality``.
    """
    copies = {
        "launcher bash array": set(_launcher_bash_list()),
        "review_state.GIT_ENV_UNSET": set(review_state.GIT_ENV_UNSET),
        "review_scope._GIT_ENV_UNSET": set(review_scope._GIT_ENV_UNSET),
        "zero_drop_git._GIT_ENV_UNSET": set(ZERO_DROP_UNSET),
    }
    for where, names in copies.items():
        offenders = sorted(set(_CONFIG_FILE_VARS) & names)
        assert not offenders, (
            f"{where} now handles {offenders}. Unsetting them loosens; pinning "
            "them removes safe.directory and lands these callers on the 'clean' "
            "nothing-staged sentinel. Harden the COMMAND instead."
        )


def test_all_four_copies_of_the_git_env_list_are_identical():
    """The launcher's bash array, both hook-side Python copies, and the original
    frozenset in ``zero_drop_git`` must hold the same names.

    FOUR copies. ``zero_drop_git._GIT_ENV_UNSET`` is the oldest of them and was
    carrying 8 of the names when this lock was written — the exact replica-drift
    this test exists to prevent, sitting unlocked in the module that invented the
    pattern.

    SCOPE, stated so the count is not read as a claim about the repo: a FIFTH,
    narrower list exists in ``scripts/worktree_lifecycle.py`` (3 names, and its
    own comment concedes it is incomplete). It is deliberately NOT in this parity
    set — it is not a review-gate decision path — and is tracked separately.
    Compared as SETS because one copy is a frozenset; the ordered copies are
    compared to each other as tuples, so a reordering is still visible.
    """
    bash = _launcher_bash_list()
    assert bash == review_state.GIT_ENV_UNSET, (
        "the launcher's bash array and review_state.GIT_ENV_UNSET have drifted"
    )
    assert review_scope._GIT_ENV_UNSET == review_state.GIT_ENV_UNSET, (
        "review_scope._GIT_ENV_UNSET and review_state.GIT_ENV_UNSET have drifted"
    )
    assert set(ZERO_DROP_UNSET) == set(review_state.GIT_ENV_UNSET), (
        "zero_drop_git._GIT_ENV_UNSET has drifted from review_state.GIT_ENV_UNSET"
    )


def test_the_attributes_hardening_is_identical_in_both_copies():
    """The flag pair is duplicated for the same reason the list is, so it gets
    the same lock — a value that drifts between the two diff sites would leave
    one classifier immune and the other not, and no list-equality test can see it.
    """
    assert review_scope._ATTRIBUTES_HARDENING == review_state._ATTRIBUTES_HARDENING, (
        "the attributes hardening has drifted between review_scope and review_state"
    )
    assert review_state._ATTRIBUTES_HARDENING[0] == "-c", (
        "the hardening must be a `-c` pair; git accepts it only BEFORE the subcommand"
    )


def test_the_list_covers_every_variable_measured_to_move_a_gate_decision():
    """A content FLOOR, so parity cannot be satisfied by four copies that agree
    on a list which has quietly lost the entries that do the work.

    These four are the ones MEASURED (2026-09-19, each variable alone) to change
    a gate decision input on its own. The list deliberately contains eight more;
    they are NOT in this floor, because they were measured to have no effect in a
    standalone-repo configuration and pinning them as required would claim more
    than is known. They stay in the scrub because adding to a scrub is the safe
    direction, and because "no effect in that configuration" is not "cannot
    matter" — GIT_COMMON_DIR inside a LINKED WORKTREE is the obvious untested
    case.
    """
    required = {
        "GIT_DIR",  # branch, and the staged hash
        "GIT_WORK_TREE",  # the worktree marker key
        "GIT_INDEX_FILE",  # the staged hash
        "GIT_EXTERNAL_DIFF",  # the staged CONTENT hash -> the escalation counter
    }
    missing = required - set(review_state.GIT_ENV_UNSET)
    assert not missing, f"scrub list lost a variable measured to move a gate: {sorted(missing)}"

    # The pin half of the floor. GIT_CONFIG_GLOBAL is the one config source
    # MEASURED to move a decision that `--no-ext-diff --no-textconv` does not
    # reach (a global core.attributesFile marking *.py binary collapses the diff
    # and the depth gate then reads `inline`), and it must be PINNED rather than
    # merely listed — unsetting it re-enables $HOME/.gitconfig.
    assert review_state._ATTRIBUTES_HARDENING[1].endswith(os.devnull), (
        "the attributes hardening lost its empty-file target; a global "
        "core.attributesFile can then mark source binary and the depth gate "
        "reads `inline` over real staged work"
    )


def test_launcher_scrubs_for_the_child_not_only_for_its_own_discovery():
    """Code-level lock on the ``exec`` line.

    The launcher scrubbed for its OWN ``git rev-parse`` discovery long before it
    scrubbed for the hook it execs, so "the array exists" is not evidence the
    child is covered. This is a substring check and a determined edit can satisfy
    it with the scrub gone (``exec "$PYTHON" … # was: env "${_GIT_ENV_SCRUB[@]}"``);
    the BEHAVIOURAL proof is in ``tests/test_scripts/test_genesis_hook_wrapper.py``
    and this is the cheap companion that names the line a refactor would drop.
    """
    exec_lines = [
        ln
        for ln in _LAUNCHER.read_text().splitlines()
        if ln.startswith("exec ") and not ln.lstrip().startswith("#")
    ]
    assert len(exec_lines) == 1, f"expected exactly one exec line, got {exec_lines}"
    assert '"${_GIT_ENV_SCRUB[@]}"' in exec_lines[0], (
        f"the launcher execs the hook WITHOUT scrubbing git env: {exec_lines[0]}"
    )


# ── every git call in both modules goes through the scrubbing helper ──────────


def _subprocess_calls(path: Path) -> list[tuple[int, bool]]:
    """``(lineno, passes_a_scrubbed_env)`` for EVERY ``subprocess.*`` call.

    Deliberately NOT "every call whose argv starts with the literal ``git``".
    That predicate was the first version of this lock and it was far weaker than
    its own docstring: it recognised ``subprocess.run(["git", ...])`` and
    ``subprocess.run(["git", *args])`` and missed eleven other spellings --
    including ``args = ["git"]; args += [...]; subprocess.run(args)``, which is
    exactly what the neighbouring gate modules
    (``review_enforcement_commit.py``, ``hooks/git_push_guard.py``) write today.
    A new call site in any missed spelling would leave the count below at 4/1
    and the suite green, which is the failure this lock exists to prevent.

    Matching ALL ``subprocess.*`` calls is both simpler and unevadable here,
    because in BOTH of these modules every subprocess call IS a git call -- 4 in
    ``review_state`` and 1 in ``review_scope``, which the count assertion below
    pins. If a non-git subprocess call is ever added to one of them this test
    fails, and the right fix is to split the predicate THEN, with the real case
    in hand, rather than to pre-weaken it now.

    Residual, stated rather than implied: ``os.popen`` and
    ``asyncio.create_subprocess_exec`` remain invisible to this walk. Neither
    appears in either module today.
    """
    tree = ast.parse(path.read_text())
    return [
        (
            node.lineno,
            any(
                kw.arg == "env"
                and isinstance(kw.value, ast.Call)
                and isinstance(kw.value.func, ast.Name)
                and kw.value.func.id in {"git_env", "_git_env"}
                for kw in node.keywords
            ),
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]


@pytest.mark.parametrize(
    ("module_name", "expected_sites"),
    [("review_state.py", 4), ("review_scope.py", 1)],
)
def test_every_subprocess_call_passes_the_scrubbed_env(module_name, expected_sites):
    """Each module's subprocess call sites all scrub -- and the COUNT is pinned.

    The count is what makes this a coverage check rather than a spot check: a
    new ``subprocess`` call fails here on arrival, whether or not whoever added
    it remembered the env, and whichever spelling they used to build the argv.
    Bump the number in the same change that adds the call site, having given it
    ``env=git_env()``.
    """
    sites = _subprocess_calls(_REPO_ROOT / "scripts" / module_name)
    assert len(sites) == expected_sites, (
        f"{module_name} now has {len(sites)} subprocess call sites, expected "
        f"{expected_sites} (at lines {[ln for ln, _ in sites]}) -- if you added "
        f"one, give it env=git_env() and update this count"
    )
    unscrubbed = [ln for ln, ok in sites if not ok]
    assert not unscrubbed, (
        f"{module_name}: subprocess call sites at lines {unscrubbed} "
        f"do not pass a scrubbed env"
    )


# ── the regression: the four measured decision inputs ────────────────────────


#: The variables this file's fixture POISONS — deliberately its own literal,
#: never ``review_state.GIT_LOCATION_VARS``. The two behavioural tests below have
#: to fail on BEHAVIOUR when the fix is absent; reaching for the constant that the
#: fix INTRODUCES makes them raise ``AttributeError`` during fixture setup
#: instead, which is red for the wrong reason — an ERROR, not a failure — and
#: would later hide a real regression behind a setup crash. MEASURED while
#: verifying this file: both tests ERRORed rather than FAILED against the unfixed
#: modules until this literal was split out.
#:
#: Three variables are enough to redirect git completely. The full list is the
#: CONTRACT, and the parity tests above are what pin it; this is the INSTRUMENT.
_POISON_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    # Scrubbed here too: the poisoned fixture below is process-wide, and a setup
    # command that inherited it would build the fixture in the WRONG repository.
    for var in _POISON_VARS:
        env.pop(var, None)
    out = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
    )
    return out.stdout


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """Repo A: on ``main`` with real staged work. Repo B: on ``feat/decoy``, clean.

    B is the repository an ambient ``GIT_DIR``/``GIT_WORK_TREE`` would redirect to.
    Every property below is "A's answer, computed while the environment points at
    B", so the two must differ in branch AND in staged state or the test cannot
    distinguish a scrub from a coincidence.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    for r in (a, b):
        r.mkdir()
        _git(r, "-c", "init.defaultBranch=main", "init", "-q")
        (r / "seed.txt").write_text("seed\n")
        _git(r, "add", "seed.txt")
        _git(r, "commit", "-qm", "seed")
    # A: 200 staged lines of never-reviewed work, in a code file so the
    # substantiality classifier sees a reviewable path rather than a doc one.
    (a / "feature.py").write_text("".join(f"x = {i}\n" for i in range(200)))
    _git(a, "add", "feature.py")
    _git(b, "checkout", "-q", "-b", "feat/decoy")
    return a, b


@pytest.fixture
def poison(repos, monkeypatch) -> Path:
    """Point git's location overrides at repo B, and PROVE the poison is live.

    The control is the load-bearing half. Without it, an environment that failed
    to reach git at all would make every assertion below pass while testing
    nothing — the scrub and an inert poison are indistinguishable from the
    results alone.
    """
    _a, b = repos
    values = {
        "GIT_DIR": str(b / ".git"),
        "GIT_WORK_TREE": str(b),
        "GIT_INDEX_FILE": str(b / ".git" / "index"),
    }
    # Keyed off the same literal the cleanup paths use, so a variable can never be
    # poisoned here and left un-cleaned there (which would leak into later tests).
    assert set(values) == set(_POISON_VARS)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return b


_EXTERNAL_DIFF_ROUTES = {
    "GIT_EXTERNAL_DIFF": {"GIT_EXTERNAL_DIFF": "/bin/true"},
    "GIT_CONFIG_COUNT": {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "diff.external",
        "GIT_CONFIG_VALUE_0": "/bin/true",
    },
    "GIT_CONFIG_PARAMETERS": {"GIT_CONFIG_PARAMETERS": "'diff.external=/bin/true'"},
    "GIT_CONFIG_GLOBAL": {"GIT_CONFIG_GLOBAL": "<evil>"},
}


@pytest.mark.parametrize("route", sorted(_EXTERNAL_DIFF_ROUTES))
def test_no_route_to_an_external_diff_can_empty_the_staged_content_hash(
    route, repos, tmp_path, monkeypatch
):
    """An external diff driver empties ``git diff --cached``, whichever door it
    arrives through — and that lands on the ``"clean"`` NOTHING-STAGED sentinel.

    Why that sentinel is the worst one to reach: ``advance_review_round`` treats
    a ``"clean"`` content hash as "not a review round" and returns the CURRENT
    round without advancing, so an ambient value here does not merely mis-size a
    review — it stops the escalation cap counting rounds at all.

    FOUR routes, because this is the finding that proved a name denylist is the
    wrong instrument for the class. Scrubbing ``GIT_EXTERNAL_DIFF`` closes
    exactly one of them; the other three set the SAME ``diff.external`` through
    git's config machinery, and a repo-local ``.git/config`` reaches it with no
    environment variable at all — which no scrub can ever intercept. What closes
    the class is ``--no-ext-diff`` on the command: git is asked not to do the
    thing, rather than asked to un-know each way of being told to.

    MEASURED 2026-09-19 across all four routes, with an ORACLE arm (no
    injection, must stay non-empty) and a NO-OP arm (bare command, must fail on
    every route). The obvious-looking alternative, ``-c diff.external=``,
    scored 0 on the ORACLE too — it sets the driver to an empty command and
    empties EVERY diff — so it would have made this gate read "clean" forever.
    """
    a, _b = repos
    cwd = str(a)

    clean = review_state._staged_content_hash(cwd=cwd)
    assert clean not in {"clean", "unknown"}, (
        f"baseline is already the degenerate value ({clean!r}); the assertion "
        f"below would pass for the wrong reason"
    )

    env = dict(_EXTERNAL_DIFF_ROUTES[route])
    if env.get("GIT_CONFIG_GLOBAL") == "<evil>":
        evil = tmp_path / "evil_gitconfig"
        evil.write_text("[diff]\n\texternal = /bin/true\n")
        env["GIT_CONFIG_GLOBAL"] = str(evil)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert review_state._staged_content_hash(cwd=cwd) == clean, (
        f"an external diff driver set through {route} changed the staged content "
        f'hash — if it reaches the "clean" sentinel the escalation cap stops '
        f"counting rounds"
    )


def test_review_scope_hardens_every_diff_it_runs(repos, tmp_path, monkeypatch):
    """The same immunity, at review_scope's single runner.

    Its classifiers decide how SUBSTANTIAL a change is by reading a diff, so an
    emptied diff reads as a trivial change and downgrades the review depth the
    commit gate then demands. Hardened in ``_git`` rather than at the six call
    sites, because a guarantee every caller must remember is a convention.
    """
    a, _b = repos
    cwd = str(a)

    clean = review_scope.classify_change_substantiality(cwd=cwd)
    assert clean == "substantial", f"baseline is not substantial ({clean!r})"

    evil = tmp_path / "evil_gitconfig"
    evil.write_text("[diff]\n\texternal = /bin/true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/bin/true")

    assert review_scope.classify_change_substantiality(cwd=cwd) == clean, (
        "an external diff driver changed the substantiality classification"
    )


def test_harden_only_touches_diff_subcommands():
    """``_harden`` must not rewrite a non-diff subcommand.

    A blanket prepend would put diff-only flags on ``rev-parse``/``merge-base``,
    which git rejects — turning every classifier into its fail-open path.
    """
    assert review_scope._harden(["diff", "-z", "--numstat"]) == [
        "-c",
        f"core.attributesFile={os.devnull}",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "-z",
        "--numstat",
    ]
    for untouched in (["rev-parse", "HEAD"], ["merge-base", "a", "b"], []):
        assert review_scope._harden(untouched) == untouched

    # ORDER is part of the contract, not cosmetic: git accepts `-c` only BEFORE
    # the subcommand, so a `-c` pair appended after `diff` makes git reject the
    # whole command — every classifier then takes its fail-open path.
    hardened = review_scope._harden(["diff", "--cached"])
    assert hardened.index("-c") < hardened.index("diff"), (
        "the -c pair must precede the subcommand or git rejects the command"
    )


def test_a_repo_local_external_diff_cannot_empty_the_staged_content_hash(repos):
    """The route NO environment scrub can ever reach — and the only test here
    that isolates ``--no-ext-diff`` as the thing doing the work.

    Every other external-diff case in this file sets an ENVIRONMENT variable, so
    it passes whether the flag is present or the scrub removed the variable:
    two mechanisms, either sufficient, neither pinned. MEASURED by mutation
    sweep — deleting ``--no-ext-diff`` left all of them green.

    ``.git/config`` is repository CONTENT. No amount of environment hygiene
    touches it, so if the flag is removed this is the assertion that fails.
    That asymmetry is also the argument for the flag over a longer denylist:
    the set of ways to reach ``diff.external`` is open, and one of them is not
    an environment variable at all.
    """
    a, _b = repos
    cwd = str(a)

    clean = review_state._staged_content_hash(cwd=cwd)
    assert clean not in {"clean", "unknown"}, f"degenerate baseline ({clean!r})"

    config = Path(a) / ".git" / "config"
    config.write_text(config.read_text() + "\n[diff]\n\texternal = /bin/true\n")

    assert review_state._staged_content_hash(cwd=cwd) == clean, (
        "a repo-local diff.external emptied the staged diff — the staged content "
        'hash fell to the "clean" sentinel and the escalation cap would stop '
        "counting rounds"
    )


def test_a_repo_local_external_diff_cannot_downgrade_substantiality(repos):
    """The same route, against review_scope's classifier.

    Its diffs decide how deep a review the commit gate demands, so an emptied
    diff reads as a trivial change. Pins ``_harden`` the way the test above pins
    the literal flags.
    """
    a, _b = repos
    cwd = str(a)

    clean = review_scope.classify_change_substantiality(cwd=cwd)
    assert clean == "substantial", f"baseline is not substantial ({clean!r})"

    config = Path(a) / ".git" / "config"
    config.write_text(config.read_text() + "\n[diff]\n\texternal = /bin/true\n")

    assert review_scope.classify_change_substantiality(cwd=cwd) == clean, (
        "a repo-local diff.external downgraded the substantiality classification"
    )


def test_a_global_attributes_file_cannot_downgrade_substantiality(repos, monkeypatch, tmp_path):
    """The fail-open the PIN exists for — and the one the diff FLAGS do not reach.

    MEASURED 2026-09-19 on 200 staged Python lines: a global
    ``core.attributesFile`` marking ``*.py binary`` collapses ``--numstat`` to
    ``-  -``, and ``classify_change_substantiality`` then reports ``inline``
    instead of ``substantial`` — the depth gate demands LESS review. This is NOT
    the ``diff.external`` class: ``--no-ext-diff --no-textconv`` do not touch it,
    which is why the scrub list alone could never have closed it and why the pin
    is protective rather than hygiene.

    The assertion is on the pinned value's EFFECT, driven through the real
    classifier with a real attributes file, rather than on ``GIT_ENV_PIN``'s
    contents — a test that only read the constant would pass against a
    ``_git_env`` that had stopped applying it.
    """
    a, _b = repos
    cwd = str(a)

    clean = review_scope.classify_change_substantiality(cwd=cwd)
    assert clean == "substantial", f"baseline is not substantial ({clean!r})"

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / "attrs").write_text("*.py binary\n")
    (fake_home / ".gitconfig").write_text(f"[core]\n\tattributesFile = {fake_home}/attrs\n")
    monkeypatch.setenv("HOME", str(fake_home))

    assert review_scope.classify_change_substantiality(cwd=cwd) == "substantial", (
        "a global core.attributesFile marking *.py binary downgraded the "
        "substantiality classification — the attributes hardening is not applied"
    )

    # CONTROL, the same one the location `poison` fixture carries and for the
    # same reason: prove the poison is LIVE in this process. Without it an
    # attributes file that never reached git — an ambient GIT_CONFIG_GLOBAL in
    # the runner, an XDG interaction, a future git that ignores attributesFile
    # for --numstat — makes the assertion above pass while testing nothing.
    monkeypatch.setattr(review_scope, "_ATTRIBUTES_HARDENING", ())
    assert review_scope.classify_change_substantiality(cwd=cwd) == "inline", (
        "the attributes poison is INERT here — with the hardening removed the "
        "classification should collapse to `inline`, so the assertion above "
        "proves nothing about the hardening"
    )


def test_git_env_overrides_cannot_re_add_a_scrubbed_variable(monkeypatch):
    """``git_env(**overrides)`` applies the scrub LAST, so an override cannot
    reinstate a scrubbed name.

    The earlier shape scrubbed first, merged second, and ``raise``d on a
    collision. That is the wrong failure direction inside a hook: an exception
    leaves the process non-zero, which Claude Code treats as NON-BLOCKING, so
    the guard written to fail loudly would have failed the gate OPEN. Ordering
    the operations makes it unconstructible instead of checked.
    """
    monkeypatch.setenv("GIT_DIR", "/nonexistent/decoy/.git")

    env = review_state.git_env()
    assert "GIT_DIR" not in env

    # The pin a real caller needs still works...
    assert review_state.git_env(COLUMNS="80")["COLUMNS"] == "80"

    # ...and a collision is dropped rather than honoured or raised.
    env = review_state.git_env(GIT_DIR="/attacker/.git")
    assert "GIT_DIR" not in env, (
        "an override re-added a scrubbed git variable — the scrub must be applied "
        "after the merge so this cannot be expressed"
    )


def test_the_poison_control_actually_redirects_git(repos, poison):
    """Instrument check: raw git run with ``cwd=A`` answers for B under the poison."""
    a, _b = repos
    out = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=a,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "feat/decoy", (
        "the poisoned environment did NOT redirect git — every other assertion in "
        "this file would pass vacuously"
    )


def test_the_four_decision_inputs_are_immune_to_an_ambient_git_env(repos, poison, monkeypatch):
    """The measurement of 2026-09-17, as a regression test.

    Each of these feeds a gate decision, and each was measured moving in the
    FAIL-OPEN direction under an ambient git environment:

      branch          main -> feat/decoy   (defeats the no-commits-to-main check)
      diff hash       <hash> -> "clean"    (200 staged lines read as nothing staged)
      worktree key    <key> -> <other>     (gate consults a marker that cannot exist)
      substantiality  substantial -> inline (downgrades the review depth required)
    """
    a, _b = repos
    cwd = str(a)

    def measure() -> dict[str, str]:
        return {
            "branch": review_state.get_current_branch(cwd=cwd),
            "diff_hash": review_state.get_current_diff_hash(cwd=cwd),
            "worktree_key": review_state._worktree_key(cwd=cwd),
            "substantiality": review_scope.classify_change_substantiality(cwd=cwd),
        }

    poisoned = measure()
    with monkeypatch.context() as clean_env:
        for var in _POISON_VARS:
            clean_env.delenv(var, raising=False)
        clean = measure()

    # Vacuity guard: a baseline that is already the degenerate/fail-open answer
    # would make the equality below true for the wrong reason.
    assert clean["branch"] == "main"
    assert clean["diff_hash"] not in {"clean", "unknown"}
    assert clean["substantiality"] == "substantial"

    assert poisoned == clean, (
        f"an ambient git environment changed a gate decision input: "
        f"poisoned={poisoned} clean={clean}"
    )
