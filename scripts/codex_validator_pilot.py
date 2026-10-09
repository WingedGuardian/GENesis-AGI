"""Fixed fixture recipes and private receipts for a supervised validator pilot.

No campaign identifiers, arbitrary test selection or ledger writes. Preview
wiring and launch/activation belong to the separately reviewed launcher slice.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import pwd
import re
import signal
import stat
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

from codex_validator_serving import child_environment, observe

from genesis.eval.qualification.evidence import (
    canonical,
    check_directory,
    digest,
    load_json,
    private_open,
    sync_directory,
)
from genesis.util.proc_kill import kill_process_group, reap_bounded
from genesis.util.streams import read_limited
from genesis.util.tasks import tracked_task

LIMIT = 64 * 1024
PROBE_TIMEOUT = 7200.0
COMMON_SOURCES = {
    "tests/conftest.py",
    "scripts/codex_validator_pilot.py",
    "scripts/codex_validator_request.py",
    "scripts/codex_validator_serving.py",
    "scripts/pr_verification.py",
}
RECIPES = {
    "peer_availability": (
        {
            "tests/test_cc/test_peer_availability.py::test_provider_refusal_is_recorded": 2,
            "tests/test_cc/test_peer_availability.py::test_local_fault_is_never_blamed_on_the_peer": 4,
            "tests/test_cc/test_peer_availability.py::test_success_clears_a_prior_block": 1,
            "tests/test_cc/test_conversation_failover.py::test_blocked_peer_survives_the_real_selection_path": 1,
            "tests/test_cc/test_conversation_failover.py::test_degenerate_empty_success_does_not_clear_a_block": 1,
        },
        {
            "src/genesis/cc/peer_availability.py",
            "src/genesis/cc/conversation.py",
            "src/genesis/cc/exceptions.py",
            "src/genesis/cc/rate_limit_reset.py",
            "tests/test_cc/conftest.py",
        },
        "Disposable peer refusal/local-fault, success and mocked failover fixtures; no live provider or canary-stream proof.",
    ),
    "exhaustion": (
        {
            "tests/test_routing/test_router.py::" + name: 1
            for name in (
                "test_exhaustion_result_carries_the_providers_it_tried",
                "test_exhaustion_event_names_the_providers_and_the_chain_size",
                "test_a_breaker_skipped_provider_is_named_on_the_exhaustion_path",
                "test_chain_size_counts_the_walkable_chain_not_the_configured_one",
                "test_the_exhaustion_LOG_LINE_names_the_providers",
            )
        },
        {"src/genesis/routing/router.py", "tests/test_routing/conftest.py"},
        "Disposable routing exhaustion/log fixtures; no live provider, deadline or full event-serialization proof.",
    ),
    "queue_snapshot": (
        {
            "tests/ego/test_queue_snapshot_contract.py::" + name: 1
            for name in (
                "test_both_ego_contexts_render_real_queue_snapshot_depths",
                "test_a_failed_queue_query_reads_unknown_never_zero",
            )
        },
        {
            "src/genesis/observability/health_data.py",
            "src/genesis/ego/context.py",
            "src/genesis/ego/genesis_context.py",
        },
        "Disposable mocked queue-depth and failed-query fixtures; no live queue census.",
    ),
}


def source_paths(recipe: str) -> set[str]:
    nodes, sources, _ = RECIPES[recipe]
    return COMMON_SOURCES | sources | {node.split("::")[0] for node in nodes}


def _private_json(path: Path) -> dict:
    check_directory(path.parent)
    with os.fdopen(private_open(path, os.O_RDONLY), "rb") as handle:
        raw = handle.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Pilot data exceeds its limit")
    value = load_json(raw)
    if not isinstance(value, dict):
        raise ValueError("Pilot data unavailable")
    return value


def configuration(workspace: Path) -> dict:
    config = _private_json(workspace / ".codex" / "pilot.json")
    if (
        set(config) != {"version", "runtime_commit", "ledger", "rows"}
        or type(config["version"]) is not int
        or config["version"] != 1
        or not isinstance(config["runtime_commit"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", config["runtime_commit"])
        or not isinstance(config["ledger"], str)
        or not Path(config["ledger"]).is_absolute()
        or not isinstance(config["rows"], list)
        or not 1 <= len(config["rows"]) <= 3
    ):
        raise ValueError("Pilot configuration unavailable")
    seen = set()
    for row in config["rows"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"pr", "repo", "merge_commit", "recipe", "intent", "sources"}
            or type(row["pr"]) is not int
            or row["pr"] <= 0
            or row["pr"] in seen
            or not isinstance(row["repo"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["repo"])
            or not isinstance(row["merge_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", row["merge_commit"])
            or not isinstance(row["recipe"], str)
            or row["recipe"] not in RECIPES
            or not isinstance(row["intent"], str)
            or not 1 <= len(row["intent"]) <= 8000
            or not isinstance(row["sources"], dict)
            or set(row["sources"]) != source_paths(row["recipe"])
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                for v in row["sources"].values()
            )
        ):
            raise ValueError("Pilot row unavailable")
        seen.add(row["pr"])
    return config


def source_hashes(runtime: Path, recipe: str) -> dict:
    hashes = {}
    for name in sorted(source_paths(recipe)):
        path = runtime / name
        if path.resolve() != path or any(p.is_symlink() for p in path.parents if p != runtime):
            raise ValueError("Pilot source unavailable")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Pilot source unavailable")
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Pilot source exceeds its limit")
        hashes[name] = hashlib.sha256(raw).hexdigest()
    return hashes


@contextmanager
def operation_lock(workspace: Path):
    """Serialize finite probes/previews, not model or supervisor waits."""
    directory = workspace / ".codex"
    check_directory(directory)
    fd = private_open(directory / "operation.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@contextmanager
def deployment_lock():
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    fd = os.open(home / ".genesis/locks/update.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Deployment lock unavailable")
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def bound_state(runtime: Path, config: dict, row: dict, *, token: str | None = None) -> dict:
    if source_hashes(runtime, row["recipe"]) != row["sources"]:
        raise ValueError("Pilot source changed")
    state = observe(runtime, token=token)
    if (
        not state["established"]
        or state["head"] != config["runtime_commit"]
        or state["serving"] != config["runtime_commit"]
    ):
        raise ValueError("Pilot deployment unavailable")
    return state


async def _stop_recipe(proc):
    # The governor forwards TERM to its separately scoped job; KILL cannot.
    kill_process_group(proc, signal.SIGTERM)
    try:
        await reap_bounded(proc)
    finally:
        kill_process_group(proc)
        await reap_bounded(proc)


async def _run_recipe(runtime: Path, recipe: str, scratch: Path) -> bytes:
    env = child_environment()
    install_home = Path(env["HOME"])
    private_home = scratch / "home"
    private_home.mkdir(mode=0o700)
    # The governor needs the real install's resource policy/guardian config.
    # Apply the private home only to pytest, after resource admission.
    env["PYTHONPATH"] = str(runtime / "src")
    python = str(runtime / ".venv/bin/python")
    junit = scratch / "results.xml"
    argv = [
        python,
        "-m",
        "genesis.hostmetrics",
        "run",
        "--name",
        "codex-validator-pilot",
        "--ram",
        "1.5",
        "--cpu",
        "100",
        "--wait-until-fits",
        "15",
        "--",
        "/usr/bin/env",
        f"HOME={private_home}",
        f"GENESIS_HOME={private_home / '.genesis'}",
        "GENESIS_PYTEST_LOCK_WAIT=1",
        f"GENESIS_PYTEST_LOCK_PATH={install_home / '.genesis/locks/pytest.lock'}",
        python,
        "-m",
        "pytest",
        *RECIPES[recipe][0],
        "-q",
        f"--junitxml={junit}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=runtime,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            out, err, code = await asyncio.gather(
                read_limited(proc.stdout, LIMIT),
                read_limited(proc.stderr, LIMIT),
                proc.wait(),
            )
        if code or out[1] > LIMIT or err[1] > LIMIT:
            raise ValueError("Pilot recipe did not complete")
        with junit.open("rb") as handle:
            raw = handle.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("Pilot results exceed their limit")
        return raw
    except BaseException:
        # Finish bounded shutdown even if the caller cancels again, then
        # propagate the original failure/cancellation. Never detach cleanup.
        stopping = tracked_task(_stop_recipe(proc), name="codex-validator-probe-stop")
        while not stopping.done():
            try:
                await asyncio.shield(stopping)
            except asyncio.CancelledError:
                continue
        stopping.result()
        raise


def measured_cases(raw: bytes, recipe: str) -> list[str]:
    expected = {
        (node.split("::")[0].removesuffix(".py").replace("/", "."), node.split("::")[1]): count
        for node, count in RECIPES[recipe][0].items()
    }
    census = Counter()
    identities = []
    # Pytest emits UTF-8. Reject declarations before stdlib parsing, so even a
    # malformed private result cannot expand a DTD/entity payload.
    text = raw.decode("utf-8", errors="strict")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ValueError("Pilot results contain unsupported declarations")
    root = ElementTree.fromstring(text)  # noqa: S314 — UTF-8, bounded, DTD/entity declarations refused
    suites = list(root)
    if (
        root.tag != "testsuites"
        or len(suites) != 1
        or suites[0].tag != "testsuite"
        or suites[0].get("tests") != str(sum(expected.values()))
        or any(suites[0].get(key) != "0" for key in ("errors", "failures", "skipped"))
    ):
        raise ValueError("Pilot suite census unavailable")
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("error", "failure", "skipped")):
            raise ValueError("Pilot recipe did not pass")
        name = case.attrib.get("name", "")
        classname = case.attrib.get("classname", "")
        census[(classname, name.split("[")[0])] += 1
        identities.append(classname + "::" + name)
    if census != expected or len(set(identities)) != len(identities):
        raise ValueError("Pilot case census unavailable")
    return sorted(identities)


def _publish_receipt(path: Path, receipt: dict):
    check_directory(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(canonical(receipt))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def packet(workspace: Path) -> dict:
    config = configuration(workspace)
    return {
        "runtime_commit": config["runtime_commit"],
        "rows": [{**row, "scope_limit": RECIPES[row["recipe"]][2]} for row in config["rows"]],
    }


def probe(workspace: Path, runtime: Path, pr: int) -> dict:
    config = configuration(workspace)
    row = next((row for row in config["rows"] if row["pr"] == pr), None)
    if row is None:
        raise ValueError("Pilot row unavailable")
    directory = workspace / ".codex/receipts"
    check_directory(directory)
    target = directory / f"{pr}.json"
    with operation_lock(workspace), deployment_lock():
        before = bound_state(runtime, config, row)
        # Early gate refusal preserves completed state; an attempted batch does not.
        target.unlink(missing_ok=True)
        sync_directory(directory)
        with tempfile.TemporaryDirectory(prefix="probe-", dir=workspace / ".codex") as temporary:
            cases = measured_cases(
                asyncio.run(_run_recipe(runtime, row["recipe"], Path(temporary))), row["recipe"]
            )
        after = bound_state(runtime, config, row, token=before["bracket"])
        receipt = {
            "version": 1,
            "configuration": digest(config),
            "row": row,
            "cases": cases,
            "before": before,
            "after": after,
            "scope_limit": RECIPES[row["recipe"]][2],
        }
        _publish_receipt(target, receipt)
    return {
        "pr": pr,
        "fixture_cases": cases,
        "scope_limit": receipt["scope_limit"],
        "receipt": digest(receipt),
        "preview_only": True,
    }
