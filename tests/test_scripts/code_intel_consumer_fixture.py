"""Owned captured-runner harness; manager/kernel facts below are synthetic.

The actual Source, queue transitions and harmless child are exercised.
OWNER_MAIN fabricates source/registration/intent and manager/kernel/cleanup/wait
witnesses; it does not exercise PhysicalOwner or qualify native containment.
"""

import inspect
import json
import os
import subprocess
from contextlib import closing
from types import SimpleNamespace

from tests.test_scripts import test_index_marker_execution as execution
from tests.test_scripts import test_index_marker_native_replay as replay
from tests.test_scripts.managed_code_intel_fixture import install_manager
from tests.test_scripts.test_code_intel_source import case

QUEUE_WRAPPER = """
_consumer_real_main = main
_consumer_original_outcome_wait = OUTCOME_LOCK_WAIT_S
if os.environ.get('TEST_SHORT_OUTCOME_WAIT') == '1':
    OUTCOME_LOCK_WAIT_S = .05
def _consumer_fault(command, real_status=None):
    raw = Path(os.environ['TEST_WIRE_PATH']).read_bytes()
    status = int(os.environ.get('TEST_FAULT_STATUS', '0'))
    assert os.write(1, raw) == len(raw)
    Path(os.environ['TEST_WIRE_PATH'] + '.injected.json').write_text(json.dumps({
        'command': command, 'raw_hex': raw.hex(), 'status': status,
        'real_status': real_status}))
    return status
def main(argv=None, *, supervisor=None):
    import io
    from contextlib import redirect_stdout
    args = sys.argv[1:] if argv is None else argv
    command = args[0]
    options = {} if supervisor is None else {'supervisor': supervisor}
    if command == 'reconcile-native' and os.environ.get('TEST_BUSY_CLAIM_RECONCILIATION') == '1':
        if get_inflight(os.environ['TEST_QUEUE_HASH']) is not None:
            Path(os.environ['TEST_RECONCILE_WITNESS']).write_text('busy after claim')
            return 75
    if command == 'automatic-ready':
        return 0 # Owned future-capability fixture only; metadata stays zero.
    if command == 'claim-native':
        if os.environ.get('TEST_WRITE_BEFORE_CLAIM'):
            write_marker(os.environ['TEST_QUEUE_REPO'], os.environ['TEST_WRITE_BEFORE_CLAIM'], 'full')
        if os.environ.get('TEST_DISABLE_BEFORE_CLAIM'):
            Path(os.environ['TEST_DISABLE_BEFORE_CLAIM']).touch()
    if command == os.environ.get('TEST_FAULT_COMMAND'):
        real_status = None
        if os.environ.get('TEST_FAULT_AFTER_REAL') == '1':
            with io.TextIOWrapper(io.BytesIO()) as sink, redirect_stdout(sink):
                real_status = _consumer_real_main(args, **options)
            assert real_status == 0
        return _consumer_fault(command, real_status)
    return _consumer_real_main(args, **options)
"""

MANAGER_OVERRIDES = """
verify_cache = lambda config: None
show = lambda unit, *props, **kwargs: {'UnitFileState': 'enabled'}
def ready(config):
    require_enabled(config)
    if not Path(config['binary']).exists():
        raise ValueError('backend unavailable')
validate_loaded_backend = lambda config: None
verified_binary = lambda path: path.open('rb')
check_backend = ready
"""

OWNER_MAIN = """
def main(snapshot, modules, raw_context, run_mode, settings_path):
    queue = modules['queue']
    expected_wait = (.05 if os.environ.get('TEST_SHORT_OUTCOME_WAIT') == '1'
                     else queue._consumer_original_outcome_wait)
    assert queue.OUTCOME_LOCK_WAIT_S == expected_wait, 'fixture changed default outcome wait'
    context = queue._native.context(json.loads(raw_context), queue._legs, queue.marker_hash)
    assert context['protocol'] == 3
    os.fstat(8) # Actual captured runner lock, not a fabricated descriptor.
    claimed = {'context': context, 'mode': run_mode}
    scenario = os.environ.get('TEST_NATIVE_SCENARIO', 'ordinary')
    if scenario == 'unstarted':
        queue.reconcile_native()
        state = queue.execution_status(context)['typed_applied_state']
    else:
        reserved = _reservation(claimed)
        assert queue.reserve_launch(context, reserved) == 'reserved'
        assert queue.register_scope(context, _registration(claimed)) == 'registered'
        assert queue.begin_native(context, _intent(queue, claimed)) == 'started'
        env = dict(os.environ, TEST_NATIVE_CONTEXT=raw_context,
                   TEST_NATIVE_DATABASE=str(queue.database_path()))
        child = subprocess.run([os.environ['TEST_PROVIDER'], context['repo_path'],
                                context['provider'], run_mode], env=env, timeout=20)
        oom = int(scenario == 'resource')
        queue.remember_native_event(context, 'terminal', _terminal(claimed, rc=child.returncode, oom=oom))
        queue.remember_native_event(context, 'cleanup', _cleanup(queue, claimed, oom=oom))
        # Compute the original controller closure without another queue read;
        # publication must retain it even while another connection holds BEGIN.
        control = {'state': 'clean', 'reason': 'normal',
            'reservation_sha256': queue._native.digest(reserved),
            'observer': reserved['owner'], 'observed_ns': time.monotonic_ns(),
            'foreground': {'identity': {**reserved['owner'], 'pid': reserved['owner']['pid'] + 1}, 'returncode': 0},
            'monitor': {'identity': {**reserved['owner'], 'pid': reserved['owner']['pid'] + 2}, 'returncode': 0},
            'stop': None}
        queue.remember_native_event(context, 'control-closure', control)
        if os.environ.get('TEST_DEFER_NATIVE_APPLY') == '1':
            return 76 # Retained facts; no outcome falsely acknowledges application.
        try:
            state = queue.apply_native(context)
        except (TimeoutError, sqlite3.OperationalError):
            return 76
    if state not in ('consumed', 'pending', 'failed'):
        return 76
    if os.environ.get('TEST_FAULT_COMMAND') == 'native-outcome':
        return queue._consumer_fault('native-outcome', 0)
    queue._write_machine([('1', 'native-outcome', context['hash'], context['provider'], raw_context, state)])
    return 0
"""


def _install_owner(path):
    builders = (
        execution._source,
        execution._reservation,
        execution._facts,
        execution._registration,
        execution._intent,
        replay._terminal,
        replay._cleanup,
    )
    path.write_text(
        path.read_text()
        + "\n"
        + "\n".join(inspect.getsource(builder) for builder in builders)
        + OWNER_MAIN
    )


def _commit_fixture(root):
    """Only the owned temporary Git repository uses synthetic author/hooks."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for args in (
        ("add", "."),
        (
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "captured consumer fixture",
        ),
    ):
        subprocess.run(["git", "-C", str(root), *args], env=env, check=True, capture_output=True)


def remove_readiness_fence(root):
    """Prepare only an owned harmless future-tail fixture, before capture."""
    path = root / 'scripts/code_intel_runner.sh'
    fence = '''protocol_rc=0
_marker automatic-ready > /dev/null 2>> "$LOG_FILE" || protocol_rc=$?
case "$protocol_rc" in
    0) _log "readiness helper reported ready, but this protocol has no ready state — stopping tick"; exit 76 ;;
    10) _log "automatic queue protocol incomplete — deferring"; exit 0 ;;
    75) _log "queue busy at readiness — deferring"; exit 0 ;;
    *) _log "automatic queue protocol observation failed — stopping tick"; exit 76 ;;
esac
'''
    body = path.read_text()
    assert body.count(fence) == 1
    path.write_text(body.replace(fence, '# Owned harmless future-tail fixture only.\n', 1))


def build_consumer(queue, tmp_path, monkeypatch, provider_setup):
    root, source, first = case(tmp_path, monkeypatch, "runner")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    lib = root / "scripts/lib"
    facade = lib / "index_marker.py"
    facade.write_text(facade.read_text() + QUEUE_WRAPPER)
    _install_owner(lib / "code_intel_owner_admission.py")
    remove_readiness_fence(root)
    provider, calls = provider_setup(tmp_path)
    # Reuse the existing settings fixture, preserving the actual managed module.
    managed = root / "scripts/codebase_managed.py"
    config_only = tmp_path / "unused-manager-entry.py"
    install_manager(config_only, tmp_path, root, provider)
    managed.write_text(managed.read_text() + MANAGER_OVERRIDES)
    queue._prepare()
    with closing(queue._connect()) as db, queue._transaction(db):
        queue._recovery.upgrade(db, queue._legs, queue._native)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            db.execute(
                "SELECT value FROM queue_meta WHERE key='automatic_protocol_ready'"
            ).fetchone()[0]
            == "0"
        )

    def environment(extra):
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "GENESIS_HOME": str(tmp_path / ".genesis"),
            "CODE_INTEL_FAKE_LOADAVG": "0",
            "CODE_INTEL_FAKE_IOWAIT": "0",
            "CODE_INTEL_FAKE_CLAUDE_CPU": "0",
            "TEST_PROVIDER": str(provider),
            "TEST_QUEUE_CLI": str(facade),
            "TEST_QUEUE_REPO": str(root),
            "TEST_QUEUE_HASH": queue.marker_hash(str(root)),
            **extra,
        }
        env.pop("CODE_INTEL_ENTRYPOINT", None)
        return env

    def run(**extra):
        _commit_fixture(root)
        snapshot = source.capture(root, "runner", first.buffers[source.SELF])
        with snapshot.sealed() as fd:
            return subprocess.run(
                snapshot.resume_command("shell", "runner"),
                stdin=fd,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment(extra),
            )

    def events():
        return (
            [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        )

    return SimpleNamespace(
        queue=queue,
        repo=str(root),
        hash=queue.marker_hash(str(root)),
        run=run,
        events=events,
        directory=tmp_path,
        runner=root / "scripts/code_intel_runner.sh",
        environment=environment,
    )


def install_provider(tmp_path):
    provider = tmp_path / "harmless_provider.sh"
    program = tmp_path / "harmless_provider.sh.py"
    calls = tmp_path / "provider.jsonl"
    program.write_text(
        "import json,os,sys,sqlite3\nfrom pathlib import Path\n"
        "if sys.argv[1] != 'fixture':\n"
        "    context=json.loads(os.environ['TEST_NATIVE_CONTEXT'])\n"
        "    db=sqlite3.connect('file:'+os.environ['TEST_NATIVE_DATABASE']+'?mode=ro',uri=True)\n"
        "    db.row_factory=sqlite3.Row\n"
        "    try:\n"
        "        row=db.execute('SELECT * FROM executions WHERE execution_seq=?',(context['execution_seq'],)).fetchone()\n"
        "        pointer=db.execute('SELECT * FROM inflight WHERE hash=?',(context['hash'],)).fetchone()\n"
        "        assert row is not None and pointer is not None and all(row[k]==v and pointer[k]==v for k,v in context.items()), 'foreign native context'\n"
        "        assert row['launch_reservation'] is not None and row['scope_registration'] is not None, 'missing owner facts'\n"
        "        assert row['native_intent'] is not None and row['launch_permission']=='spent', 'child before durable intent'\n"
        "    finally: db.close()\n"
        f"with Path(os.environ.get('TEST_PROVIDER_LOG',{str(calls)!r})).open('a') as stream:\n"
        "    stream.write(json.dumps({'repo':sys.argv[1],'provider':sys.argv[2],'mode':sys.argv[3]})+'\\n')\n"
        "if os.environ.get('TEST_PROVIDER_READY'):\n"
        "    import time\n"
        "    Path(os.environ['TEST_PROVIDER_READY']).write_text('provider reached')\n"
        "    deadline=time.monotonic()+15\n"
        "    while not Path(os.environ['TEST_PROVIDER_ALLOW']).exists():\n"
        "        if time.monotonic()>deadline: raise RuntimeError('owned provider barrier expired')\n"
        "        time.sleep(.01)\n"
        "if os.environ.get('TEST_ENQUEUE_AT'):\n"
        "    import subprocess\n"
        f"    step=len(Path({str(calls)!r}).read_text().splitlines())\n"
        "    if str(step)==os.environ['TEST_ENQUEUE_AT']:\n"
        "        subprocess.run([sys.executable,os.environ['TEST_QUEUE_CLI'],'write','--repo',sys.argv[1],'--tools',sys.argv[2],'--mode','fast'],check=True,capture_output=True)\n"
        "sys.exit(int(os.environ.get('TEST_'+sys.argv[2].upper()+'_RC','0')))\n"
    )
    provider.write_text('#!/usr/bin/env bash\nexec /usr/bin/python3 "$0.py" "$@"\n')
    provider.chmod(0o700)
    control_log = tmp_path / "instrument.jsonl"
    control = subprocess.run(
        ["bash", str(provider), "fixture", "gitnexus", "fast"],
        env={**os.environ, "TEST_PROVIDER_LOG": str(control_log), "TEST_GITNEXUS_RC": "0"},
        capture_output=True,
        timeout=10,
    )
    assert control.returncode == 0 and not calls.exists(), control.stderr
    assert [json.loads(line) for line in control_log.read_text().splitlines()] == [
        {"repo": "fixture", "provider": "gitnexus", "mode": "fast"}
    ]
    return provider, calls
