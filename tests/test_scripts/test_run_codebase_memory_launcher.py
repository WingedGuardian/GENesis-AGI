"""Managed MCP registration and missing-setup refusal, without a native manager."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _REPO_ROOT / ".claude/mcp/run-codebase-memory"
_REGISTER_LIB = _REPO_ROOT / "scripts/lib/mcp_register.sh"
_SYSTEM_PATH = "/usr/bin:/bin"


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.mark.parametrize("override", ["none", "binary", "memory", "sentinel", "path"])
def test_missing_setup_has_no_legacy_fallback(tmp_path, override):
    log = tmp_path / "raw.log"
    binary = _write_exec(tmp_path / "codebase-memory-mcp", f"#!/bin/sh\necho raw > {log}\n")
    env = {"PATH": _SYSTEM_PATH, "HOME": str(tmp_path),
           "VENV_PATH": str(Path(sys.executable).parent.parent)}
    if override == "binary":
        env["CODEBASE_MEMORY_MCP_BIN"] = str(binary)
    elif override == "memory":
        env["CODEBASE_MEMORY_MCP_MEMORY_MAX"] = "lots"
    elif override == "sentinel":
        env["CODEBASE_MEMORY_MCP_DISABLE_FILE"] = str(tmp_path / "absent")
    elif override == "path":
        env["PATH"] = str(tmp_path) + ":" + _SYSTEM_PATH
    result = subprocess.run(["/bin/bash", str(_LAUNCHER)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1 and "managed Codebase refused" in result.stderr
    assert "codebase-managed.json" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("kind", ["missing", "directory", "nonexecutable", "dangling", "relative", "newline", "carriage-return"])
def test_launcher_refuses_unavailable_selected_interpreter(tmp_path, kind):
    venv = tmp_path / "selected"
    interpreter = venv / "bin/python"
    interpreter.parent.mkdir(parents=True)
    if kind == "directory":
        interpreter.mkdir()
    elif kind == "nonexecutable":
        interpreter.write_text("not executable")
    elif kind == "dangling":
        interpreter.symlink_to(tmp_path / "absent")
    raw = {"relative": "relative", "newline": str(venv) + "\n", "carriage-return": str(venv) + "\r"}.get(kind, str(venv))
    result = subprocess.run(["/bin/bash", str(_LAUNCHER)], env={"PATH": _SYSTEM_PATH, "HOME": str(tmp_path), "VENV_PATH": raw},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "managed Codebase refused" in result.stderr
    assert "codebase-managed.json" not in result.stderr


@pytest.mark.parametrize("literal", ["space path", "$HOME", "%n %%", "quotes'\"", "back\\slash", "λ雪", "$(touch marker)", "`touch marker`"])
def test_launcher_custom_literal_interpreter_reaches_actual_helper(tmp_path, literal):
    venv = tmp_path / literal
    interpreter = venv / "bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(["/bin/bash", str(_LAUNCHER)], env={"PATH": _SYSTEM_PATH, "HOME": str(home), "VENV_PATH": str(venv)},
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "managed Codebase refused" in result.stderr
    assert str(home / ".genesis/config/codebase-managed.json") in result.stderr
    assert not (tmp_path / "marker").exists()


# ── _register_mcp drift-healing (sources the REAL shared lib) ─────────────


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("existing", [False, True])
def test_actual_registration_loop_without_path_provider(tmp_path, renderer, existing):
    import json

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "claude.log"
    _write_exec(fakebin / "claude", f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
    home = tmp_path / "home"
    home.mkdir()
    (home / ".genesis").mkdir()
    (home / ".genesis/codebase-memory-mcp.disabled").touch()
    if existing:
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "codebase-memory-mcp": {"command": "codebase-memory-mcp"}}}))
    source = (_REPO_ROOT / "scripts" / renderer).read_text()
    if renderer == "install.sh":
        start = source.index("# Register code intelligence tools as MCP servers")
        end = source.index("# Queue initial code intelligence indexing", start)
    else:
        start = source.index("# --- MCP Server Registration (Code Intelligence) ---")
        end = source.index("# --- Code Intelligence Indexing ---", start)
    env = {"PATH": str(fakebin) + ":" + _SYSTEM_PATH, "HOME": str(home),
           "SCRIPT_DIR": str(_REPO_ROOT / "scripts"), "REPO_DIR": str(tmp_path),
           "GENESIS_ROOT": str(tmp_path), "GENESIS_GREP_MCP_URL": ""}
    result = subprocess.run(["/bin/bash", "-euc", source[start:end]], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert f"mcp add codebase-memory-mcp -s user -- {tmp_path}/.claude/mcp/run-codebase-memory" in calls
    if existing:
        assert "mcp remove codebase-memory-mcp -s user" in calls
    assert "enable" not in calls and "start" not in calls


def _run_register(tmp_path: Path, args: list[str], claude_json: dict | None,
                  mcp_list: str = "") -> tuple[subprocess.CompletedProcess, Path]:
    import json
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    clog = tmp_path / "claude.log"
    _write_exec(
        fakebin / "claude",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{clog}"\n'
        f'if [ "$1 $2" = "mcp list" ]; then cat "{tmp_path}/mcp_list.txt" 2>/dev/null; fi\n'
        "exit 0\n",
    )
    (tmp_path / "mcp_list.txt").write_text(mcp_list, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if claude_json is not None:
        (home / ".claude.json").write_text(json.dumps(claude_json), encoding="utf-8")
    harness = tmp_path / "harness.sh"
    quoted = " ".join(f"'{a}'" for a in args)
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_REGISTER_LIB}"\n_register_mcp {quoted}\n',
        encoding="utf-8",
    )
    res = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(home)},
        capture_output=True, text=True, timeout=30,
    )
    return res, clog


def test_register_user_scope_fresh_adds(tmp_path):
    res, clog = _run_register(tmp_path, ["srv", "user", "/opt/launcher"], {"mcpServers": {}})
    assert res.returncode == 0
    assert "mcp add srv -s user -- /opt/launcher" in clog.read_text()


def test_register_user_scope_same_basename_is_noop(tmp_path):
    # Registered by bare name, stored resolved — same basename is NOT drift.
    cfg = {"mcpServers": {"gitnexus": {"command": "/home/x/.local/bin/gitnexus"}}}
    res, clog = _run_register(tmp_path, ["gitnexus", "user", "gitnexus", "mcp"], cfg)
    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert not clog.exists() or "mcp add" not in clog.read_text()


def test_register_user_scope_drift_reregisters(tmp_path):
    cfg = {"mcpServers": {"codebase-memory-mcp":
                          {"command": "/home/x/.local/bin/codebase-memory-mcp"}}}
    res, clog = _run_register(
        tmp_path, ["codebase-memory-mcp", "user", "/repo/.claude/mcp/run-codebase-memory"], cfg,
    )
    assert res.returncode == 0
    calls = clog.read_text()
    assert "mcp remove codebase-memory-mcp -s user" in calls
    assert "mcp add codebase-memory-mcp -s user -- /repo/.claude/mcp/run-codebase-memory" in calls
    assert calls.index("mcp remove") < calls.index("mcp add")


def test_register_user_scope_absolute_path_drift_reregisters(tmp_path):
    # Same basename, different absolute path (e.g. a launcher registered from
    # a since-reaped worktree) IS drift for an absolute intended command.
    stale = "/repo/.claude/worktrees/old/.claude/mcp/run-codebase-memory"
    cfg = {"mcpServers": {"codebase-memory-mcp": {"command": stale}}}
    res, clog = _run_register(
        tmp_path,
        ["codebase-memory-mcp", "user", "/repo/.claude/mcp/run-codebase-memory"], cfg,
    )
    assert res.returncode == 0
    calls = clog.read_text()
    assert "mcp remove codebase-memory-mcp -s user" in calls
    assert "mcp add codebase-memory-mcp -s user -- /repo/.claude/mcp/run-codebase-memory" in calls


def test_register_warns_on_local_scope_shadow(tmp_path):
    # A local-scope (per-project) entry takes precedence over user scope and
    # must be surfaced, never silently shadowed. It is warned, not removed.
    cfg = {
        "mcpServers": {"codebase-memory-mcp":
                       {"command": "/repo/.claude/mcp/run-codebase-memory"}},
        "projects": {"/some/project": {"mcpServers": {
            "codebase-memory-mcp": {"command": "/old/bare-binary"}}}},
    }
    res, clog = _run_register(
        tmp_path,
        ["codebase-memory-mcp", "user", "/repo/.claude/mcp/run-codebase-memory"], cfg,
    )
    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert "LOCAL-scope" in res.stdout
    assert "/old/bare-binary" in res.stdout
    assert "mcp remove" not in (clog.read_text() if clog.exists() else "")


def test_register_project_scope_existing_is_noop(tmp_path):
    res, clog = _run_register(
        tmp_path, ["serena", "project", "serena", "start-mcp-server"],
        None, mcp_list="serena: serena start-mcp-server\n",
    )
    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert "mcp add" not in clog.read_text()


# -- _register_mcp_http (sources the REAL shared lib) ----------------------
# The HTTP path shares no code with the stdio one above: different add flags,
# a different stored key ("url", never "command"), and no argv to compare. It
# was verified by hand against a live config during development and left no
# regression behind, which an external reviewer flagged. These cover it.


def _run_register_http(tmp_path: Path, args: list[str], claude_json: dict | None,
                       env_extra: dict | None = None,
                       ) -> tuple[subprocess.CompletedProcess, Path]:
    import json
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    clog = tmp_path / "claude.log"
    _write_exec(
        fakebin / "claude",
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{clog}"\n'
        "exit 0\n",
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if claude_json is not None:
        (home / ".claude.json").write_text(json.dumps(claude_json), encoding="utf-8")
    harness = tmp_path / "harness_http.sh"
    quoted = " ".join(f"'{a}'" for a in args)
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_REGISTER_LIB}"\n_register_mcp_http {quoted}\n',
        encoding="utf-8",
    )
    env = {"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(home)}
    env.update(env_extra or {})
    res = subprocess.run(
        ["bash", str(harness)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    return res, clog


def test_http_fresh_adds_with_transport_flag(tmp_path):
    """Argument ORDER and the --transport flag are the contract with the CLI."""
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"], {"mcpServers": {}})
    assert res.returncode == 0
    assert "mcp add --transport http grep-app -s user https://mcp.grep.app" in clog.read_text()


def test_http_matching_url_is_noop(tmp_path):
    """Idempotence: the same URL must not churn the registration."""
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"],
        {"mcpServers": {"grep-app": {"type": "http", "url": "https://mcp.grep.app"}}})
    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert "mcp add" not in (clog.read_text() if clog.exists() else "")


def test_http_preserves_an_entry_of_another_transport(tmp_path):
    """Never delete configuration Genesis cannot account for.

    The sibling of test_http_url_mismatch_preserves_and_warns: a stdio entry
    mismatches on a different axis (no url at all rather than a different one)
    and must take the same branch. Removing it would destroy the operator's
    command/args/env. Flagged by an external reviewer after an earlier
    revision removed it.
    """
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"],
        {"mcpServers": {"grep-app": {"type": "stdio", "command": "/opt/their-own"}}})
    assert res.returncode == 0
    log = clog.read_text() if clog.exists() else ""
    assert "mcp remove" not in log, "must not delete an entry Genesis did not create"
    assert "mcp add" not in log, "must not register over the operator's entry"
    assert "WARNING" in res.stdout
    assert "has NOT modified it" in res.stdout


def test_http_empty_url_declines_and_says_entry_is_still_live(tmp_path):
    """Declining must not read as 'absent' when the server is still registered."""
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", ""],
        {"mcpServers": {"grep-app": {"type": "http", "url": "https://mcp.grep.app"}}})
    assert res.returncode == 0
    assert "remains ACTIVE" in res.stdout
    assert "mcp add" not in (clog.read_text() if clog.exists() else "")


def test_http_empty_url_with_no_entry_is_a_plain_skip(tmp_path):
    res, clog = _run_register_http(tmp_path, ["grep-app", "user", ""], {"mcpServers": {}})
    assert res.returncode == 0
    assert "registration declined" in res.stdout
    assert "remains ACTIVE" not in res.stdout


def test_http_url_default_is_declinable_by_empty_env(tmp_path):
    """`${VAR-default}` not `:=` — an explicitly empty value must survive.

    The colon form treats empty as unset and would re-assign the public
    endpoint, defeating the one spelling an operator reaches for to opt out.
    """
    harness = tmp_path / "probe.sh"
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_REGISTER_LIB}"\necho "URL=[${{GENESIS_GREP_MCP_URL}}]"\n',
        encoding="utf-8",
    )
    res = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": _SYSTEM_PATH, "HOME": str(tmp_path), "GENESIS_GREP_MCP_URL": ""},
        capture_output=True, text=True, timeout=30,
    )
    assert "URL=[]" in res.stdout

    res_default = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": _SYSTEM_PATH, "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    assert "URL=[https://mcp.grep.app]" in res_default.stdout


def test_http_url_mismatch_preserves_and_warns(tmp_path):
    """NEVER heal a URL mismatch — an existing entry is the operator's.

    Owner decision 2026-09-24, after three revisions each tried to decide
    OWNERSHIP from the stored value and each produced one defect: heal-always
    destroyed an operator's server, preserve-always made the override inert,
    and rename-then-heal destroyed an operator's server one name over. Nothing
    in ~/.claude.json records who wrote an entry, so the predicate is deleted
    rather than narrowed again.

    The accepted cost is that GENESIS_GREP_MCP_URL does not take effect by
    itself on an already-registered box; the warning must therefore print BOTH
    urls and the exact command that adopts ours, or the operator cannot act.
    """
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.internal.example"],
        {"mcpServers": {"grep-app": {"type": "http", "url": "https://mcp.grep.app"}}})

    assert res.returncode == 0
    log = clog.read_text() if clog.exists() else ""
    assert "mcp remove" not in log, "an existing entry is never removed"
    assert "mcp add" not in log, "and never registered over"
    assert "DIFFERENT URL" in res.stdout
    # Both sides named: without them the operator cannot tell what changed.
    assert "stored: https://mcp.grep.app" in res.stdout
    assert "ours:   https://mcp.internal.example" in res.stdout
    assert "claude mcp remove grep-app -s user" in res.stdout


_SHADOWED = {
    "mcpServers": {"grep-app": {"type": "http", "url": "https://mcp.grep.app"}},
    "projects": {
        "/home/op/thing": {
            "mcpServers": {"grep-app": {"type": "http", "url": "https://shadow.example"}}
        }
    },
}


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("https://mcp.internal.example", "preserve branch: 'remove, then re-run'"),
        ("", "decline branch: 'remains ACTIVE'"),
    ],
)
def test_http_early_returns_still_warn_about_a_local_shadow(tmp_path, url, why):
    """Every exit that CLAIMS something about what is live owes this warning.

    Local scope outranks user scope, so both remedies these branches offer are
    incomplete where a local entry exists: removing the user-scope entry does
    not make ours reachable, and "remains ACTIVE" is not what a session reaches.

    Caught by an adversarial review of the never-heal change. The preserve
    branch is a REGRESSION that change introduced -- the old heal path fell
    THROUGH to the shared warning at the bottom of the function, and converting
    it to an early return took the warning with it. The call site is untouched
    context in the diff, so nothing about the hunk showed the loss. The decline
    branch never had it. Measured before the fix: deleting the HTTP warning
    outright left the suite 28/28 green, so nothing bound it here at all.
    """
    res, _clog = _run_register_http(tmp_path, ["grep-app", "user", url], _SHADOWED)

    assert res.returncode == 0
    assert "LOCAL-scope registrations that shadow" in res.stdout, why
    assert "/home/op/thing -> https://shadow.example" in res.stdout
    assert "-s local" in res.stdout, "the remedy must name the local scope"


def test_http_same_url_wrong_transport_is_not_already_registered(tmp_path):
    """Transport is part of the identity, not just the URL.

    MEASURED on CC 2.1.246: `claude mcp add --transport sse` stores
    {"type": "sse", "url": "https://mcp.grep.app"} -- the SAME url our http
    registration uses. Matching on url alone reported "already registered" for
    an entry that cannot reach an http-only endpoint, so code search was
    silently absent while the installer said it was fine.

    Preserve-and-warn still applies: we do not replace their entry, we name
    the conflict. Flagged by an external reviewer.
    """
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"],
        {"mcpServers": {"grep-app": {"type": "sse", "url": "https://mcp.grep.app"}}})

    assert res.returncode == 0
    assert "already registered" not in res.stdout, (
        "an sse entry at our url cannot serve an http-only endpoint"
    )
    assert "DIFFERENT TRANSPORT" in res.stdout
    assert "stored: sse" in res.stdout
    log = clog.read_text() if clog.exists() else ""
    assert "mcp remove" not in log, "still never replaces the operator's entry"
    assert "mcp add" not in log


def test_http_untyped_entry_at_our_url_is_still_already_registered(tmp_path):
    """Negative control for the transport check: a missing type is NOT sse.

    `--transport sse` always records a type, so an entry with none is a legacy
    http registration. Rejecting it would preserve-and-warn on every bootstrap
    run forever on a box set up by an older Claude Code -- noise with no
    hazard behind it.
    """
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"],
        {"mcpServers": {"grep-app": {"url": "https://mcp.grep.app"}}})

    assert res.returncode == 0
    assert "already registered" in res.stdout
    assert "mcp add" not in (clog.read_text() if clog.exists() else "")


def test_http_preserve_shadow_warning_compares_against_OURS(tmp_path):
    """The shadow check must ask "after they adopt ours, will this shadow it?"

    An adversarial review proposed comparing against the STORED url
    (`${registered:-$url}`) instead, on the reasoning that the stored entry is
    what is live right now. MEASURED that this is wrong in the one case that
    matters, which is the case below: a local entry equal to the operator's
    OWN url. Comparing against the stored value goes SILENT there -- yet after
    the operator follows this branch's remedy, the user-scope entry is OURS and
    that local entry shadows it. The remedy would not work and nothing said so.

    A mutation sweep found the suite could not tell the two spellings apart, so
    this exists to pin the choice rather than leave it to the next reader.
    """
    theirs = "https://theirs.example"
    res, _clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://ours.example"],
        {"mcpServers": {"grep-app": {"type": "http", "url": theirs}},
         "projects": {"/p": {"mcpServers": {
             "grep-app": {"type": "http", "url": theirs}}}}})

    assert res.returncode == 0
    assert "LOCAL-scope registrations that shadow" in res.stdout, (
        "a local entry matching the operator's OWN url will still shadow ours "
        "once they adopt it — comparing against the stored value hides that"
    )
    assert f"/p -> {theirs}" in res.stdout


@pytest.mark.parametrize("stored", [{}, None], ids=["empty-dict", "null"])
def test_http_preserves_an_entry_that_is_present_but_falsy(tmp_path, stored):
    """Membership, not truthiness: `{}` is still a name the operator owns.

    The detector used to answer "absent" for a falsy value and send the caller
    to `claude mcp add`. That is non-destructive TODAY only because the real
    CLI refuses a taken name (MEASURED on CC 2.1.246: exit 1, "already exists
    in user config", entry byte-identical) — a third party's behaviour holding
    up a guarantee this function makes in its own name.
    """
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"],
        {"mcpServers": {"grep-app": stored}})

    assert res.returncode == 0
    assert "mcp add" not in (clog.read_text() if clog.exists() else "")
    assert "has NOT modified it" in res.stdout


def test_http_decline_does_not_claim_an_entry_is_live_it_could_not_read(tmp_path):
    """`unknown` is not `present` -- the decline branch must not assert either.

    _mcp_entry_present is THREE-valued ("1" / "" / "unknown"). The decline
    branch tested it with `[ -n ... ]`, which is true for "unknown" too, so an
    unreadable ~/.claude.json produced "an EXISTING registration remains
    ACTIVE" -- a positive claim about state it had just failed to read -- plus
    an instruction to remove an entry that may not exist.

    Found by an external reviewer on the commit that introduced the helper:
    one of its two call sites learned about the third value and the other did
    not. The preserve branch handled `unknown` from the start.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".claude.json").write_text("{not json at all", encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    _write_exec(fakebin / "claude", "#!/usr/bin/env bash\nexit 0\n")
    harness = tmp_path / "harness_decline_unknown.sh"
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_REGISTER_LIB}"\n'
        "_register_mcp_http 'grep-app' 'user' ''\n",
        encoding="utf-8",
    )
    res = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(home)},
        capture_output=True, text=True, timeout=30,
    )

    assert res.returncode == 0
    assert "remains ACTIVE" not in res.stdout, (
        "must not assert a live registration from a config it could not read"
    )
    assert "remove it with" not in res.stdout, (
        "must not tell the operator to remove an entry it cannot confirm exists"
    )
    assert "UNKNOWN" in res.stdout
    assert "claude mcp list" in res.stdout, "say how to find out instead"


def test_http_decline_still_reports_a_confirmed_entry_as_active(tmp_path):
    """Negative control: a READABLE config with an entry must still say ACTIVE.

    The fix above must narrow the claim, not delete it -- an operator who
    declines while a registration is genuinely live still needs telling.
    """
    res, _clog = _run_register_http(
        tmp_path, ["grep-app", "user", ""],
        {"mcpServers": {"grep-app": {"type": "http", "url": "https://mcp.grep.app"}}})

    assert res.returncode == 0
    assert "remains ACTIVE" in res.stdout
    assert "UNKNOWN" not in res.stdout


def test_http_unreadable_config_does_not_claim_the_name_is_free(tmp_path):
    """Fail CLOSED: a config we cannot parse is not evidence the name is free.

    Both reads in this function swallow stderr, so before the fix a corrupt
    or unreadable ~/.claude.json collapsed to "no entry" and the function
    proceeded straight to `claude mcp add` over whatever was really there.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".claude.json").write_text("{not json at all", encoding="utf-8")
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    clog = tmp_path / "claude.log"
    _write_exec(
        fakebin / "claude",
        "#!/usr/bin/env bash\n" f'echo "$*" >> "{clog}"\n' "exit 0\n",
    )
    harness = tmp_path / "harness_corrupt.sh"
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_REGISTER_LIB}"\n'
        "_register_mcp_http 'grep-app' 'user' 'https://mcp.grep.app'\n",
        encoding="utf-8",
    )
    res = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(home)},
        capture_output=True, text=True, timeout=30,
    )

    assert res.returncode == 0
    assert not clog.exists(), "must not register against a config it cannot read"
    assert "could not read" in res.stdout
    assert "NOTHING was changed" in res.stdout


def test_http_absent_config_is_a_genuine_fresh_install(tmp_path):
    """The fail-closed read must not brick a fresh box that has no config yet.

    The negative control for the test above: FileNotFoundError is the ordinary
    first-install state and has to read as "name is free", or install.sh never
    registers anything on a new machine.
    """
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"], None)

    assert res.returncode == 0
    assert "mcp add --transport http grep-app -s user https://mcp.grep.app" in clog.read_text()


def test_http_url_mismatch_cannot_strand_the_name_when_add_fails(tmp_path):
    """The remove-then-add window does not exist, rather than being handled.

    An external reviewer's finding: a failing `claude mcp add` after a
    successful `remove` leaves the operator with no code search at all.

    Under never-heal this path returns before `claude` is invoked AT ALL, so
    "add fails" is not a state it can reach. The fake exits 1 to make that
    explicit — if a revision ever reintroduces a call here, that is the
    failure the operator would get, and the invocation assertion below fires
    first regardless.

    (An earlier docstring claimed `claude` was "failing on every invocation";
    an adversarial review measured that there are ZERO invocations, so the
    exit-1 fake was inert and the claim was false.)
    """
    import json as _json

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    clog = tmp_path / "claude.log"
    _write_exec(
        fakebin / "claude",
        "#!/usr/bin/env bash\n" f'echo "$*" >> "{clog}"\n' "exit 1\n",
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    cfg = {"mcpServers": {"grep-app": {"type": "http", "url": "https://mcp.grep.app"}}}
    (home / ".claude.json").write_text(_json.dumps(cfg), encoding="utf-8")
    harness = tmp_path / "harness_fail.sh"
    harness.write_text(
        f'#!/usr/bin/env bash\n. "{_REGISTER_LIB}"\n'
        "_register_mcp_http 'grep-app' 'user' 'https://mcp.internal.example'\n",
        encoding="utf-8",
    )
    res = subprocess.run(
        ["bash", str(harness)],
        env={"PATH": f"{fakebin}:{_SYSTEM_PATH}", "HOME": str(home)},
        capture_output=True, text=True, timeout=30,
    )

    assert res.returncode == 0
    # The binding claim, and one its sibling does not make: the preserve path
    # invokes `claude` ZERO times, so there is no ordering of remove/add that
    # could strand the name. A reviewer measured that asserting the config is
    # unchanged proves nothing here — the fake only echoes, so nothing in the
    # harness could write that file either way.
    assert not clog.exists(), "the preserve path must not invoke `claude` at all"


def test_http_does_not_touch_a_plain_grep_entry(tmp_path):
    """An operator's own `grep` server is not ours and must be left alone.

    grep.app's own install command registers `grep`. Genesis deliberately does
    not claim that name, so a box carrying both ends up with the operator's
    `grep` untouched and Genesis's `grep-app` alongside it.
    """
    res, clog = _run_register_http(
        tmp_path, ["grep-app", "user", "https://mcp.grep.app"],
        {"mcpServers": {"grep": {"type": "http", "url": "https://their-own.example"}}})

    assert res.returncode == 0
    log = clog.read_text() if clog.exists() else ""
    assert "mcp remove" not in log, "must never touch the operator's `grep`"
    assert "mcp add --transport http grep-app" in log, "ours still registers"
