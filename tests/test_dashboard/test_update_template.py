"""Verify the orchestrator template is valid Python after substitution."""

import ast
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.dashboard.routes.updates import _ORCHESTRATOR_TEMPLATE

pytestmark = pytest.mark.usefixtures("updates_sandbox")


def test_orchestrator_template_compiles():
    """Substituted template must be syntactically valid Python.

    Catches typos, indentation errors, or broken string literals that
    would otherwise only surface at runtime during an actual update.
    """
    code = _ORCHESTRATOR_TEMPLATE.format(
        summary_file="/tmp/test_summary.txt",
        escalation_file="/tmp/test_escalation.txt",
        refused_file="/tmp/test_refused.json",
        pid_file="/tmp/test_pid",
        genesis_root="/tmp/test_genesis",
        tier1_prompt="test tier 1 prompt",
        tier2_prompt="test tier 2 prompt",
        gh_credential_env=("GH_TOKEN",),
    )
    # compile() raises SyntaxError on invalid Python
    compile(code, "<orchestrator-template>", "exec")


def test_backup_tab_surfaces_not_restarted_success():
    """The Last Update status span must distinguish a not-restarted success."""
    from pathlib import Path

    from genesis.dashboard.routes import updates

    html = (
        Path(updates.__file__).parent.parent
        / "templates" / "partials" / "tabs" / "backup.html"
    ).read_text()
    assert "server not restarted" in html
    assert "server_restarted === false" in html
    # A failed row reconciled to success must be labelled, not plain green.
    assert "reconciled" in html
    assert " (reconciled)" in html


def _rendered_spawn_cc_source() -> str:
    import ast
    import string

    from genesis.dashboard.routes import updates

    fields = {f for _, f, _, _ in string.Formatter().parse(updates._ORCHESTRATOR_TEMPLATE) if f}
    rendered = updates._ORCHESTRATOR_TEMPLATE.format(**{f: "x" for f in fields})
    [spawn] = [
        n
        for n in ast.walk(ast.parse(rendered))
        if isinstance(n, ast.FunctionDef) and n.name == "spawn_cc"
    ]
    return ast.unparse(spawn)


def test_orchestrator_sessions_carry_the_update_tier_stamp():
    """Tier 1/2 sessions resolve the update's merge in the main checkout; the
    main-checkout guard allows exactly the sessions carrying this stamp."""
    src = _rendered_spawn_cc_source()
    assert "'GENESIS_UPDATE_TIER': '1'" in src, src
    assert "env=env" in src, src


def test_the_tier3_session_carries_the_update_tier_stamp(monkeypatch, tmp_path):
    from genesis.dashboard.routes import updates

    captured = {}

    class _Proc:
        pid = 1

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)
    monkeypatch.delenv("GENESIS_UPDATE_TIER", raising=False)
    updates._spawn_detached_cc("p", "opus", "max")
    assert captured["env"]["GENESIS_UPDATE_TIER"] == "1"


# ── no update session carries a gh credential ────────────────────────────────
#
# The update pipeline starts `claude -p ... --dangerously-skip-permissions` from
# two places: Tier 3 from the route (_spawn_detached_cc) and Tiers 1/2 from the
# generated orchestrator script (spawn_cc). Both reuse the invoker's credential
# set, and Tier 3 passes the invoker's own launch gate.

_SESSION_PINS = {"GENESIS_UPDATE_TIER": "1", "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "0"}


def _credential_names() -> tuple[str, ...]:
    from genesis.cc import invoker

    names = invoker._GH_CREDENTIAL_ENV
    # Guard the loops below: they are over the invoker's set, and an empty set
    # would make every one of them pass.
    assert "GH_TOKEN" in names and "GITHUB_TOKEN" in names, names
    return names


def _server_env(monkeypatch, inherited: bool) -> None:
    """The server's own environment, with or without the credentials in it."""
    for name in _credential_names():
        if inherited:
            monkeypatch.setenv(name, "tok")
        else:
            monkeypatch.delenv(name, raising=False)


def _fake_claude(tmp_path: Path) -> tuple[Path, Path]:
    """A `claude` that records the environment it was started with. Returns its
    directory and the record. ``<unset>`` marks a variable that was absent, which
    the launch gate treats differently from one that is present and empty."""
    bindir, dump = tmp_path / "bin", tmp_path / "claude-env"
    bindir.mkdir()
    reported = (*_credential_names(), *_SESSION_PINS)
    fake = bindir / "claude"
    fake.write_text(
        "#!/bin/sh\n{\n"
        + "".join(f"printf '%s=%s\\n' {n} \"${{{n}-<unset>}}\"\n" for n in reported)
        + f"}} > {shlex.quote(str(dump))}\n"
    )
    fake.chmod(0o755)
    return bindir, dump


def _reported_env(dump: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in dump.read_text().splitlines())


def _expected_session_env() -> dict[str, str]:
    return {**dict.fromkeys(_credential_names(), ""), **_SESSION_PINS}


@pytest.mark.parametrize("inherited", [True, False], ids=["inherited", "absent"])
def test_the_tier3_session_carries_no_gh_credential(monkeypatch, tmp_path, inherited):
    """Present and empty either way: a value the server inherited is cleared, and
    a name it never had is still pinned, so the gate has a key to vouch for."""
    from genesis.cc import invoker
    from genesis.dashboard.routes import updates

    captured = {}

    class _Proc:
        pid = 1

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)
    _server_env(monkeypatch, inherited)
    updates._spawn_detached_cc("p", "opus", "max")
    env = captured["env"]
    expected = _expected_session_env()
    assert {k: env.get(k, "<unset>") for k in expected} == expected
    invoker._assert_no_gh_credentials(env)


def test_the_tier3_spawn_gates_the_env_it_launches_with(monkeypatch, tmp_path):
    """The gate is the last step before the process: it sees the very dict the
    session starts with, and nothing changes that dict afterwards."""
    from genesis.dashboard.routes import updates

    events = []

    class _Proc:
        pid = 1

    def gate(env):
        events.append(("gate", env, dict(env)))

    def fake_popen(cmd, **kw):
        events.append(("popen", kw["env"], dict(kw["env"])))
        return _Proc()

    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)
    # raising=False: against a routes module with no gate, fail on the order
    # below (the gate never ran), not on the patch.
    monkeypatch.setattr(updates, "_assert_no_gh_credentials", gate, raising=False)
    updates._spawn_detached_cc("p", "opus", "max")
    assert [e[0] for e in events] == ["gate", "popen"]
    (_, gated, gated_copy), (_, launched, launched_copy) = events
    assert gated is launched
    assert gated_copy == launched_copy


def test_a_tier3_env_that_fails_the_gate_starts_nothing(monkeypatch, tmp_path):
    """With the route's own pins switched off, what is left is the invoker's
    gate, which reads the invoker's set: it refuses, no process starts, and the
    previous session's log is not truncated."""
    from genesis.dashboard.routes import updates

    started = []
    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(updates, "_GH_CREDENTIAL_ENV", (), raising=False)
    _server_env(monkeypatch, inherited=True)
    log = tmp_path / ".genesis" / "cc_session_opus.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("previous run")
    with pytest.raises(RuntimeError, match="Refusing to launch"):
        updates._spawn_detached_cc("p", "opus", "max")
    assert started == []
    assert log.read_text() == "previous run"


def test_the_tier3_process_starts_with_no_gh_credential(monkeypatch, tmp_path):
    """End to end: the route's real Popen starts a real process (a fake `claude`,
    first on PATH), and that process reports the environment it received."""
    from genesis.dashboard.routes import updates

    bindir, dump = _fake_claude(tmp_path)
    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setattr(updates.subprocess, "Popen", subprocess.Popen)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.delenv("GENESIS_UPDATE_TIER", raising=False)
    _server_env(monkeypatch, inherited=True)
    proc = updates._spawn_detached_cc("p", "opus", "max")
    assert proc.wait(timeout=60) == 0
    assert _reported_env(dump) == _expected_session_env()


def _supervised_orchestrator_script(monkeypatch, sandbox) -> Path:
    """The script the supervised start writes, with its launch replaced: the
    real render, by the real caller, of the real template."""
    from flask import Flask

    from genesis.dashboard.routes import updates

    class _Proc:
        pid = 4242

        def kill(self):
            pass

    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: _Proc())
    with Flask(__name__).app_context():
        updates._apply_supervised(sandbox.genesis_dir / "update_in_progress.pid")
    [script] = (sandbox.home / "tmp").glob("genesis-update-orch-*.py")
    return script


def test_the_orchestrator_script_carries_the_invokers_credential_set(
    monkeypatch, updates_sandbox
):
    """Substituted from the invoker at render time, not typed into the template:
    a name added to the invoker's set reaches Tiers 1/2 with no second edit."""
    from genesis.cc import invoker

    tree = ast.parse(_supervised_orchestrator_script(monkeypatch, updates_sandbox).read_text())
    baked = [
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "GH_CREDENTIAL_ENV" for t in n.targets)
    ]
    assert baked == [invoker._GH_CREDENTIAL_ENV]


@pytest.mark.parametrize("inherited", [True, False], ids=["inherited", "absent"])
def test_an_orchestrator_session_starts_with_no_gh_credential(
    monkeypatch, tmp_path, updates_sandbox, inherited
):
    """End to end for Tiers 1/2: the script the supervised start wrote runs as
    its own process, starts a fake `claude` (the only one on its PATH), and that
    session reports the environment it received. The orchestrator's own
    environment is left as the server's, credentials included."""
    from genesis.cc import invoker

    script = _supervised_orchestrator_script(monkeypatch, updates_sandbox)
    bindir, dump = _fake_claude(tmp_path)
    names = _credential_names()
    env = {k: v for k, v in os.environ.items() if k not in names}
    env.pop("GENESIS_UPDATE_TIER", None)
    env.update(HOME=str(tmp_path / "isolated-home"), PATH=f"{bindir}:/usr/bin:/bin")
    if inherited:
        env.update(dict.fromkeys(names, "tok"))
    subprocess.run([sys.executable, str(script)], check=True, capture_output=True, env=env, timeout=60)
    reported = _reported_env(dump)
    assert reported == _expected_session_env()
    invoker._assert_no_gh_credentials(reported)
