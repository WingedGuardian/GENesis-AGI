"""The Bash-allowlist guard injected into dispatched sessions.

A scoped background profile declares ``GENESIS_BASH_ALLOWLIST``; something has
to READ it. Until this guard existed the only reader was the global user-level
chokepoint, which this repository wires nowhere — so on a fresh clone the
restriction was declared and enforced by nobody.

Four properties, and the suite is arranged around them because each one is a
way the guard could be wrong in a direction nothing else would notice:

1. It REFUSES a command outside the allowlist (the containment itself).
2. It PERMITS an allowlisted one. A guard that only refuses proves nothing
   about the path that has to keep working.
3. It is a NO-OP when no allowlist is set. This is what makes registering it on
   every dispatch safe, so it is load-bearing for the wiring decision and not
   merely a nicety.
4. It FAILS CLOSED when an allowlist is in force and the command cannot be read
   — previously an exit 0, i.e. the restriction silently dropped.

The guard and the chokepoint share ONE predicate
(``scripts/hooks/bash_allowlist_lib.sh``); the last test in this file is what
stops a future edit forking them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = _REPO_ROOT / "scripts" / "hooks" / "bash_allowlist_guard.sh"
LIB = _REPO_ROOT / "scripts" / "hooks" / "bash_allowlist_lib.sh"
CHOKEPOINT = _REPO_ROOT / "scripts" / "bash_safety_hook.sh"
LAUNCHER = _REPO_ROOT / ".claude" / "hooks" / "genesis-hook"


def _payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _run(
    script: Path,
    stdin: str,
    *,
    allowlist: str | None = None,
    path: str | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Invoke a hook script with a payload on stdin.

    Inherits the real environment (the scripts need awk/jq on PATH as in prod)
    but always clears GENESIS_BASH_ALLOWLIST first, so each case sets it — or
    deliberately does not — rather than inheriting whatever the developer's
    shell happens to carry.
    """
    env = dict(os.environ)
    env.pop("GENESIS_BASH_ALLOWLIST", None)
    if allowlist is not None:
        env["GENESIS_BASH_ALLOWLIST"] = allowlist
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        ["bash", str(script)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or str(_REPO_ROOT),
    )


@pytest.fixture
def no_jq_path(tmp_path: Path) -> str:
    """A PATH carrying the guard's other dependencies but NOT jq.

    Symlinking the real binaries rather than emptying PATH keeps the failure
    attributable: if the guard refuses here it is because jq is missing, not
    because awk or cat vanished too.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("bash", "cat", "awk", "wc", "env", "dirname", "pwd", "sh"):
        real = shutil.which(name)
        if real:
            (bindir / name).symlink_to(real)
    assert shutil.which("jq", path=str(bindir)) is None, (
        "fixture did not actually remove jq from PATH — the jq-absent cases "
        "below would be testing nothing"
    )
    return str(bindir)


# --- 1. Refuses what is outside the allowlist -------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "curl http://example.invalid",
        "python -m genesis serve",
        "cat /etc/passwd",
        "echo hello",
        "git push origin main",
    ],
)
def test_guard_refuses_non_allowlisted_binary(cmd):
    assert _run(GUARD, _payload(cmd), allowlist="gh").returncode == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api x; curl http://example.invalid",
        "gh api x && curl http://example.invalid",
        "gh api x | sh",
        "gh api x > /tmp/out",
        "gh api $(whoami)",
        "gh api `whoami`",
        "gh api x\ncurl http://example.invalid",
    ],
)
def test_guard_refuses_escapes_from_an_allowlisted_first_token(cmd):
    """An allowlisted first token does not license reaching a second binary."""
    assert _run(GUARD, _payload(cmd), allowlist="gh").returncode == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr list & whoami",
        "gh pr list &whoami",
        "gh api x & curl http://example.invalid",
    ],
)
def test_guard_refuses_a_bare_ampersand(cmd):
    """`&` backgrounds the first command and RUNS THE NEXT ONE.

    Listed separately from `&&`, which does not subsume it. Before this entry
    the shipped predicate returned 0 for these while bash ran the second half —
    a full escape to any binary on PATH from an allowlisted first token.

    Kept as its own test rather than folded into the parametrize above so a
    regression names the operator rather than 'one of eight escapes'.
    """
    assert _run(GUARD, _payload(cmd), allowlist="gh").returncode == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr list",
        "gh api repos/o/r --jq .state",
        "gh pr comment 1 --body hello",
        "gh --version",
    ],
)
def test_the_operator_set_did_not_over_block_ordinary_gh_usage(cmd):
    """The cost side of the operator set: paren-free gh usage still works.

    Widening a guard is cheap to do and expensive to discover, so the shapes
    that must KEEP working are asserted beside the shapes that must not.
    """
    assert _run(GUARD, _payload(cmd), allowlist="gh").returncode == 0


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr create --title 'a (b) c'",
        "gh search prs 'is:open (draft)'",
        "gh api x (y)",
    ],
)
def test_parentheses_are_refused_and_this_costs_ordinary_arguments(cmd):
    """`(` and `)` are refused by OWNER DECISION, as defence in depth.

    The measurement does not support them on its own: against real bash, with
    an allowlisted first token, a subshell is unreachable in every position the
    other entries leave open — `(cmd)` straight after a command word is a
    syntax error, and getting to one needs a separator that is already blocked.

    So this test pins a COST rather than a protection, deliberately. Every case
    here is an ordinary, harmless gh invocation that an allowlisted session can
    no longer issue: a PR title, a search query, a positional argument. If that
    cost is ever judged too high, this test is the list of what comes back, and
    the enumeration in bash_allowlist_lib.sh is the argument for removing them.
    """
    assert _run(GUARD, _payload(cmd), allowlist="gh").returncode == 2


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api x\ncurl http://example.invalid",
        "gh api x\ngh api y",
        'gh pr comment 1 --body "a\nb"',
    ],
)
def test_guard_refuses_a_multi_line_command_as_multi_line(cmd):
    """Bind the newline branch by its MESSAGE, because its VERDICT is subsumed.

    A mutation sweep found this branch unbound, and measuring both arms says
    why: the exit code is 2 with the branch and 2 without it, for every
    multi-line shape. awk prints the first field of EVERY line, so `$first`
    becomes "gh\\ncurl" and matches no allowlist entry — the first-token check
    catches these by accident. Only the stderr differs:

        enabled   rc=2  "BLOCKED: multi-line commands are not permitted ..."
        disabled  rc=2  "BLOCKED: this session may only run [gh] ..."

    So an exit-code assertion would pass against a deleted branch. Assert the
    reason, which is the one thing this branch uniquely produces.

    A TRAILING newline is deliberately not a case here: `$(...)` strips trailing
    newlines, so `gh api x\\n` reaches the predicate as `gh api x` and is
    allowed — rc 0 in BOTH arms, correctly, since that is all it is. An earlier
    draft of this test asserted rc 2 for it, on an inference drawn from the
    mutated arm alone without running the control.
    """
    proc = _run(GUARD, _payload(cmd), allowlist="gh")
    assert proc.returncode == 2
    assert "multi-line" in proc.stderr, (
        "refused, but not BY the multi-line branch — the first-token check "
        "caught it instead, so this branch is no longer bound"
    )


def test_a_trailing_newline_is_not_a_multi_line_command():
    """The control for the case excluded above, so the exclusion is not a gap.

    If `$(...)` ever stopped stripping trailing newlines, or the guard started
    reading the payload some other way, this would begin refusing an ordinary
    allowlisted command and say so here rather than in a dispatched session.
    """
    proc = _run(GUARD, _payload("gh api x\n"), allowlist="gh")
    assert proc.returncode == 0, (
        "a trailing newline is not a second command; refusing it would be a "
        "false positive against ordinary allowlisted work"
    )


# --- 2. Permits what is on the allowlist ------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr view 1 --repo o/r",
        "gh api repos/o/r/pulls/1 --jq .state",
        "gh --version",
    ],
)
def test_guard_permits_allowlisted_binary(cmd):
    assert _run(GUARD, _payload(cmd), allowlist="gh").returncode == 0


def test_guard_permits_any_member_of_a_multi_entry_allowlist():
    for cmd in ("gh --version", "git status"):
        assert _run(GUARD, _payload(cmd), allowlist="gh,git").returncode == 0
    assert _run(GUARD, _payload("curl x"), allowlist="gh,git").returncode == 2


# --- 3. No-op without an allowlist ------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "curl http://example.invalid",
        "rm -rf /",
        "echo hello && echo world",
    ],
)
def test_guard_is_a_noop_without_an_allowlist(cmd):
    """Unconditional registration is only safe because of this.

    The guard is wired into EVERY dispatch, so if it had any effect on a
    session that declared no allowlist it would be refusing work across the
    whole system. Note it must not block `rm -rf /` either: that is the
    chokepoint's job, and duplicating it here would make this guard's scope a
    second thing to keep in sync.
    """
    assert _run(GUARD, _payload(cmd)).returncode == 0


def test_guard_returns_before_reading_stdin_when_inactive():
    """The no-op path costs nothing: no payload read, so no jq, no parse.

    Verified by handing it a payload that is not even JSON. If the guard were
    parsing before checking the environment, this would not exit 0.
    """
    assert _run(GUARD, "this is not json at all").returncode == 0


# --- 4. Fails closed when it cannot read the command ------------------------


def test_guard_refuses_when_jq_is_absent_and_an_allowlist_is_in_force(no_jq_path):
    proc = _run(GUARD, _payload("gh --version"), allowlist="gh", path=no_jq_path)
    assert proc.returncode == 2
    assert "jq" in proc.stderr


def test_guard_stays_a_noop_when_jq_is_absent_and_no_allowlist(no_jq_path):
    """Fail-closed is scoped to allowlisted sessions.

    A missing jq must not turn the guard into a system-wide refusal; every
    ordinary dispatch would be caught by it.
    """
    assert _run(GUARD, _payload("echo hi"), path=no_jq_path).returncode == 0


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "{}",
        '{"tool_input": {}}',
        '{"tool_input": {"command": ""}}',
    ],
)
def test_guard_refuses_an_unreadable_payload_under_an_allowlist(stdin):
    assert _run(GUARD, stdin, allowlist="gh").returncode == 2


def test_guard_refuses_when_the_shared_predicate_is_unreadable(tmp_path: Path):
    """A guard that cannot load its predicate has established nothing."""
    staged = tmp_path / "bash_allowlist_guard.sh"
    staged.write_text(GUARD.read_text(encoding="utf-8"), encoding="utf-8")
    # Deliberately do NOT copy the lib alongside it.
    proc = _run(staged, _payload("gh --version"), allowlist="gh")
    assert proc.returncode == 2
    assert "unreadable" in proc.stderr


# --- The chokepoint's matching fail-closed leg ------------------------------


def test_chokepoint_refuses_an_unreadable_payload_under_an_allowlist(no_jq_path):
    """Previously exit 0 — the restriction dropped in silence.

    The chokepoint reads the command with jq and treated an empty result as
    "nothing to act on". That is right in general and wrong under an allowlist,
    where it means the guard cannot see what it is supposed to be confining.
    """
    proc = _run(
        CHOKEPOINT,
        _payload("curl http://example.invalid"),
        allowlist="gh",
        path=no_jq_path,
    )
    assert proc.returncode == 2


def test_chokepoint_still_exits_zero_on_an_unreadable_payload_without_an_allowlist(
    no_jq_path,
):
    """Back-compat: the fail-closed leg must not fire for ordinary sessions."""
    assert _run(CHOKEPOINT, "", path=no_jq_path).returncode == 0


# --- The launcher has to be able to run a shell hook ------------------------


def test_launcher_runs_a_shell_hook_under_bash(tmp_path: Path):
    """Every hook wired today is Python, and the launcher exec'd $PYTHON
    unconditionally — so a .sh hook routed through it died on its shebang.

    Routing the guard through the launcher (rather than wiring its absolute
    path) is what gives it the same HOOK_ROOT anti-drift resolution as the
    other hooks, so this dispatch has to work.
    """
    probe = _REPO_ROOT / "scripts" / "hooks" / "_launcher_probe_tmp.sh"
    probe.write_text("#!/usr/bin/env bash\necho SHELL_HOOK_RAN\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            [str(LAUNCHER), "hooks/_launcher_probe_tmp.sh"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            env={**os.environ, "GENESIS_HOOK_DEV_LOCAL": "1"},
        )
    finally:
        probe.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    assert "SHELL_HOOK_RAN" in proc.stdout


@pytest.mark.parametrize(
    ("allowlist", "want", "why"),
    [
        ("gh", 2, "an allowlisted session must not get a non-blocking exit"),
        (None, 1, "an ordinary session keeps the non-blocking error"),
    ],
)
def test_launcher_refuses_a_missing_containment_hook_without_dev_local(
    tmp_path: Path, allowlist, want, why
):
    """The production resolution path — NO ``GENESIS_HOOK_DEV_LOCAL``.

    This is the gap an adversarial review found, and the reason it found it is
    that every other launcher test here passes the dev-local override, which
    BYPASSES the MAIN-worktree resolution. So the production path was untested
    by construction, and the E2E evidence gathered with the override did not
    describe it either.

    The failure it hides: the launcher resolves a hook against the MAIN
    worktree while the registrar checks the INVOKING tree. When those diverge —
    a rollout, a main checkout that has not pulled, a --separate-git-dir clone
    that trips the MAIN_ROOT fallback — the launcher cannot find the guard and
    exits 1. Claude Code treats a non-2 exit as a NON-BLOCKING error, so the
    command proceeds and the session runs unconfined while every declaration
    says otherwise.

    The fixture is a non-git directory, so MAIN_ROOT resolves empty and
    HOOK_ROOT falls back to the root itself — deterministic, and it exercises
    the same branch without needing the override.
    """
    fake_root = tmp_path / "root"
    (fake_root / ".claude" / "hooks").mkdir(parents=True)
    (fake_root / "scripts" / "hooks").mkdir(parents=True)
    (fake_root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(LAUNCHER, fake_root / ".claude" / "hooks" / "genesis-hook")
    assert not (fake_root / "scripts" / "hooks" / "bash_allowlist_guard.sh").exists()

    env = {k: v for k, v in os.environ.items() if k != "GENESIS_HOOK_DEV_LOCAL"}
    env.pop("GENESIS_BASH_ALLOWLIST", None)
    if allowlist is not None:
        env["GENESIS_BASH_ALLOWLIST"] = allowlist

    proc = subprocess.run(
        [
            str(fake_root / ".claude" / "hooks" / "genesis-hook"),
            "hooks/bash_allowlist_guard.sh",
        ],
        input=_payload("whoami"),
        capture_output=True,
        text=True,
        cwd=str(fake_root),
        env=env,
    )
    assert proc.returncode == want, f"{why}; got {proc.returncode}: {proc.stderr}"


def test_launcher_runs_a_shell_hook_without_resolving_the_venv(tmp_path: Path):
    """A shell hook must not be gated on the Python venv existing.

    The launcher exits 1 when it cannot find the venv, and Claude Code treats a
    non-2 exit as a NON-BLOCKING error — so the tool call proceeds. For a shell
    hook whose whole job is to refuse things, being gated on an unrelated
    interpreter is a fail-open on any box where the venv is missing or
    mid-rebuild. Resolution is therefore skipped for a `.sh` hook.

    Simulated by pointing the launcher at a tree with no `.venv`, which is what
    the roots loop scans for.
    """
    fake_root = tmp_path / "root"
    (fake_root / ".claude" / "hooks").mkdir(parents=True)
    (fake_root / "scripts" / "hooks").mkdir(parents=True)
    shutil.copy2(LAUNCHER, fake_root / ".claude" / "hooks" / "genesis-hook")
    probe = fake_root / "scripts" / "hooks" / "probe.sh"
    probe.write_text("#!/usr/bin/env bash\necho NO_VENV_OK\n", encoding="utf-8")
    assert not (fake_root / ".venv").exists(), "fixture must have no venv"

    proc = subprocess.run(
        [str(fake_root / ".claude" / "hooks" / "genesis-hook"), "hooks/probe.sh"],
        capture_output=True,
        text=True,
        cwd=str(fake_root),
        env={**os.environ, "GENESIS_HOOK_DEV_LOCAL": "1"},
    )
    assert proc.returncode == 0, f"shell hook gated on the venv: {proc.stderr}"
    assert "NO_VENV_OK" in proc.stdout


def test_launcher_still_runs_a_python_hook_under_python(tmp_path: Path):
    """The negative control for the dispatch above.

    Without this, a `case` that sent everything to bash would pass the test
    above while breaking all ~55 wired hooks.
    """
    probe = _REPO_ROOT / "scripts" / "hooks" / "_launcher_probe_tmp.py"
    probe.write_text("import sys\nprint('PY_HOOK_RAN', sys.version_info[0])\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            [str(LAUNCHER), "hooks/_launcher_probe_tmp.py"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            env={**os.environ, "GENESIS_HOOK_DEV_LOCAL": "1"},
        )
    finally:
        probe.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    assert "PY_HOOK_RAN 3" in proc.stdout


# --- One predicate, two entry points ----------------------------------------


def test_both_entry_points_use_the_shared_predicate():
    """The drift guard.

    Two places can intercept a dispatched session's Bash, and if they ever
    disagree the containment depends on which one an install happens to have
    wired. Assert the shared function is what both call, and that neither has
    re-grown an inline copy of the allowlist test.
    """
    lib = LIB.read_text(encoding="utf-8")
    assert "genesis_bash_allowlist_verdict()" in lib, (
        "the shared predicate changed name — update both callers and this test"
    )

    for caller in (GUARD, CHOKEPOINT):
        text = caller.read_text(encoding="utf-8")
        assert "bash_allowlist_lib.sh" in text, f"{caller.name} no longer sources the predicate"
        assert "genesis_bash_allowlist_verdict" in text, f"{caller.name} no longer calls it"

    # The first-token test is the heart of the predicate. If a literal copy of
    # it reappears in a caller, the two have forked.
    marker = '*",$first,"*'
    assert marker in lib
    for caller in (GUARD, CHOKEPOINT):
        assert marker not in caller.read_text(encoding="utf-8"), (
            f"{caller.name} carries its own copy of the allowlist test — the predicate has forked"
        )


# --- The chokepoint's fail-closed leg is scoped to the Bash tool ------------


@pytest.mark.parametrize(
    ("tool", "payload_extra", "want", "why"),
    [
        ("BashOutput", {"bash_id": "abc"}, 0, "carries no command and is not this gate's business"),
        ("KillShell", {"shell_id": "abc"}, 0, "same"),
        ("Bash", {}, 2, "a Bash call with no readable command must still fail closed"),
    ],
)
def test_chokepoint_fail_closed_leg_is_scoped_to_the_bash_tool(tool, payload_extra, want, why):
    """A matcher is a REGEX, so a bare "Bash" also matches "BashOutput".

    MEASURED: a hook registered as `^Bash$` fires on a Bash call, so the
    unanchored spelling — which is how this hook is wired where it is wired at
    all — matches sibling tools too. Those carry no `.tool_input.command`, and
    without this scoping the new fail-closed leg would refuse every one of them
    in an allowlisted session, reporting a missing command the tool never had.

    The injected guard avoids this by anchoring its own matcher; the chokepoint
    cannot, because its registration lives in an install's user-level settings
    and is not ours to change. So it gates on the tool name instead.
    """
    payload = json.dumps({"tool_name": tool, "tool_input": payload_extra})
    proc = _run(CHOKEPOINT, payload, allowlist="gh")
    assert proc.returncode == want, f"{tool}: {why} (stderr: {proc.stderr[:120]})"
