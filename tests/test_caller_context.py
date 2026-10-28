"""``genesis.caller_context``: the accessor, its request scope, and the CI guard.

The guard is the reason the accessor exists. A shared MCP process serves many
callers, so a reader that consults the process environment directly sees "no
dispatch marker" and passes a human-only gate for every caller. The guard
fails on any direct read of an identity variable under ``src/`` outside the
accessor module.

What the guard sees: ``os.environ.get/pop/setdefault``, ``os.environ[...]``,
``os.getenv`` and ``... in os.environ``, keyed by a string literal or by a
module-level constant holding one. A bare ``os.environ.pop(NAME, None)``
statement clears a value and is not a read, so it is not flagged.

Two more nets close what that scan cannot see (an aliased ``os``, a copied
mapping, a loop variable, a keyword argument):

- a literal that IS an identity name (a string, or bytes such as
  ``b"NAME=1"`` matched against another process's ``/proc`` environ) may
  appear only in the accessor, the child-env writer modules and one listed
  peer-environ reader (this caught the loop-keyed read on main);
- the accessor's exported name constants may be used only in the accessor and a
  short allowlist, so a module cannot key a read by importing them instead.

Blind spots, stated so nobody reads a clean run as proof: a name assembled from
pieces at run time (or fetched with ``getattr`` by a string), any read inside an
allowlisted module that is not a literal ``os.environ`` read (those modules are
reviewed by hand and listed below with what each one does), and everything
outside ``src/genesis``: hook scripts and launchers under ``scripts/`` read
these variables by design and are not scanned, so a shared service must not
import them for identity.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from genesis import caller_context as cc
from genesis.caller_context import (
    IDENTITY_ENV_VARS,
    CallerContext,
    caller_context,
    request_caller,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "genesis"
_ACCESSOR = _SRC / "caller_context.py"
_ACCESSOR_CONSTANTS = {
    name
    for name, value in vars(cc).items()
    if isinstance(value, str) and value in IDENTITY_ENV_VARS
}


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    for var in IDENTITY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ── the accessor ─────────────────────────────────────────────────────────────


def test_env_fallback_reads_raw_values_per_call(monkeypatch):
    assert caller_context() == CallerContext(dispatched=False)
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("GENESIS_SESSION_SUPERVISED", "true")
    monkeypatch.setenv("GENESIS_SESSION_ID", "")
    monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "first_party")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", " t ")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy")
    ctx = caller_context()
    assert ctx == CallerContext(
        dispatched=True,
        supervised=False,
        session_id="",
        origin="first_party",
        transcript_id=" t ",
        legacy_session_id="legacy",
    )
    assert ctx.unsupervised_dispatched is True
    monkeypatch.setenv("GENESIS_SESSION_SUPERVISED", "1")
    assert caller_context().unsupervised_dispatched is False


def test_request_context_overrides_env_and_resets(monkeypatch):
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    request = CallerContext(dispatched=False, session_id="req", profile="interactive")
    with request_caller(request) as active:
        assert active is request
        assert caller_context() is request
    assert caller_context().dispatched is True
    assert caller_context().profile is None


def test_request_context_resets_after_an_exception():
    with pytest.raises(RuntimeError), request_caller(CallerContext(session_id="req")):
        raise RuntimeError("tool failed")
    assert caller_context().session_id is None


def test_request_contexts_nest():
    with request_caller(CallerContext(session_id="outer")):
        with request_caller(CallerContext(session_id="inner")):
            assert caller_context().session_id == "inner"
        assert caller_context().session_id == "outer"
    assert caller_context().session_id is None


async def test_request_context_resets_on_cancellation():
    entered = asyncio.Event()

    async def call():
        with request_caller(CallerContext(session_id="req")):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(call())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert caller_context().session_id is None


async def test_concurrent_requests_see_their_own_caller():
    seen: dict[str, str | None] = {}
    both_inside = asyncio.Barrier(2)

    async def call(sid: str):
        with request_caller(CallerContext(session_id=sid)):
            await both_inside.wait()
            seen[sid] = caller_context().session_id

    await asyncio.gather(call("a"), call("b"))
    assert seen == {"a": "a", "b": "b"}


def test_a_partly_built_context_fails_closed_at_the_human_gate():
    """A field left out must not grant authority: the defaults are least privileged."""
    from genesis.security.immunity_shadow import DispatchGateRefused, guard_human_gate

    with request_caller(CallerContext(profile="restricted")), pytest.raises(DispatchGateRefused):
        guard_human_gate("probe")


async def test_request_caller_reaches_every_migrated_reader(db, monkeypatch, tmp_path):
    """With the environment empty, a request-scoped caller drives each reader.

    The characterization suite proves the env path is unchanged; this proves the
    readers consult the accessor at call time rather than a snapshot of the
    environment, which is the property a shared service depends on.
    """
    from unittest.mock import AsyncMock, patch

    from genesis.db.crud import follow_ups
    from genesis.eval.j9_hooks import _resolve_session_id
    from genesis.mcp.health import direct_session_tools, follow_up_tools
    from genesis.mcp.health import session_charter_tools as charter
    from genesis.memory.provenance import session_origin_from_env
    from genesis.security.immunity_shadow import is_dispatched_session_env
    from genesis.trash import trash

    request = CallerContext(
        dispatched=True,
        supervised=False,
        session_id="r",
        origin="first_party",
        transcript_id="t",
        legacy_session_id=None,
    )
    item = tmp_path / "victim.txt"
    item.write_text("x")
    enqueue = AsyncMock(return_value="q-1")
    monkeypatch.setattr(direct_session_tools, "_db", object())
    with request_caller(request):
        assert is_dispatched_session_env() is True
        assert session_origin_from_env() == "first_party"
        assert charter._is_dispatched() is True
        assert charter._default_added_by() == "ambient"
        assert charter._own_session_ids() == {"t", "r"}
        assert _resolve_session_id(None) == "r"
        assert trash(item, reason="probe", caller="test").session_id == "r"
        with patch("genesis.db.crud.direct_session_queue.enqueue", enqueue):
            await direct_session_tools._impl_direct_session_run("p", deliver_to_origin=True)
        with patch.object(follow_up_tools, "_get_db", return_value=db):
            res = await follow_up_tools._impl_follow_up_create(
                content="request item", reason="r", strategy="ego_judgment", work_state="ready"
            )
    assert enqueue.await_args.kwargs["origin_session_id"] == "r"
    assert (await follow_ups.get_by_id(db, res["id"]))["source"] == "ego_dispatch"


def test_identity_constants_cover_the_closed_set():
    assert set(vars(cc)[name] for name in _ACCESSOR_CONSTANTS) == IDENTITY_ENV_VARS


# ── the CI guard ─────────────────────────────────────────────────────────────


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _module_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and isinstance(stmt.value.value, str):
                    constants[target.id] = stmt.value.value
    return constants


def _key_name(node: ast.AST | None, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in constants:
            return constants[node.id]
        if node.id in _ACCESSOR_CONSTANTS:
            return vars(cc)[node.id]
    if isinstance(node, ast.Attribute) and node.attr in _ACCESSOR_CONSTANTS:
        return vars(cc)[node.attr]
    return None


def direct_identity_reads(source: str, filename: str = "<src>") -> list[tuple[int, str]]:
    """Every line that reads an identity variable straight from ``os.environ``."""
    tree = ast.parse(source, filename)
    constants = _module_constants(tree)
    clearing = {
        id(stmt.value)
        for stmt in ast.walk(tree)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
    }
    hits: list[tuple[int, str]] = []

    def record(node: ast.AST, key: ast.AST | None) -> None:
        name = _key_name(key, constants)
        if name in IDENTITY_ENV_VARS:
            hits.append((node.lineno, name))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            key = node.args[0] if node.args else None
            key = next((kw.value for kw in node.keywords if kw.arg == "key"), key)
            if (
                _is_os_environ(func.value)
                and func.attr in {"get", "setdefault"}
                or _is_os_environ(func.value)
                and func.attr == "pop"
                and id(node) not in clearing
                or (
                    func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
            ):
                record(node, key)
        elif (
            isinstance(node, ast.Subscript)
            and _is_os_environ(node.value)
            and isinstance(node.ctx, ast.Load)
        ):
            record(node, node.slice)
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
        ):
            if any(_is_os_environ(c) for c in node.comparators):
                record(node, node.left)
    return hits


def test_no_direct_identity_reads_outside_the_accessor():
    offenders = []
    files = sorted(_SRC.rglob("*.py"))
    assert len(files) > 100, f"scanned only {len(files)} files under {_SRC}"
    for path in files:
        if path == _ACCESSOR:
            continue
        for line, name in direct_identity_reads(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"{path.relative_to(_SRC.parent)}:{line} reads {name}")
    assert not offenders, (
        "Read caller identity through genesis.caller_context.caller_context(), "
        "never from os.environ (a shared MCP process would pass every caller "
        "through the human-only gates):\n" + "\n".join(offenders)
    )


# Modules that WRITE identity into a child's environment (a copied ``env`` dict,
# or clearing a stale value in-server). Writing is how identity is stamped, not
# a read of the caller, so these may name the variables.
_ENV_WRITERS = {
    "cc/invoker.py": "CCInvoker._build_env stamps every CC child",
    "experimentation/cc_router.py": "builds an experiment child's env",
    "session_awareness/headless.py": "builds a headless child's env",
    "runtime/_core.py": "clears a stale origin from the server process",
}


# Modules that read ANOTHER process's environment, an independent signal and
# not the caller's own claim. A shared service needs its own decision for each.
_PEER_ENV_READERS = {
    "payments/peer_check.py": "reads a peer's /proc environ to decide process ancestry",
}


def _names_identity(value: object) -> str | None:
    if isinstance(value, str) and value in IDENTITY_ENV_VARS:
        return value
    if isinstance(value, bytes):
        for name in IDENTITY_ENV_VARS:
            raw = name.encode()
            if value == raw or value.startswith(raw + b"="):
                return name
    return None


def identity_name_literals(source: str, filename: str = "<src>") -> list[tuple[int, str]]:
    """Every literal that is an identity variable's name (str, or ``b"NAME"``/``b"NAME=..."``)."""
    return [
        (node.lineno, name)
        for node in ast.walk(ast.parse(source, filename))
        if isinstance(node, ast.Constant) and (name := _names_identity(node.value))
    ]


def test_identity_names_appear_only_in_the_accessor_and_env_writers():
    """Second net, for what the read scan cannot see.

    A read keyed by a loop variable over a tuple of names (the shape
    ``_own_session_ids`` had) or by a key assembled elsewhere is invisible to
    the read scan, but the name still has to be written down somewhere. Only
    the accessor and the child-env writers may write it.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if path == _ACCESSOR or rel in _ENV_WRITERS or rel in _PEER_ENV_READERS:
            continue
        for line, name in identity_name_literals(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"src/genesis/{rel}:{line} names {name}")
    assert not offenders, (
        "Only genesis/caller_context.py (readers) and the child-env writers may name "
        "an identity variable; import the constant or call caller_context():\n"
        + "\n".join(offenders)
    )


def test_env_writer_allowlist_has_no_stale_entries():
    for rel in {**_ENV_WRITERS, **_PEER_ENV_READERS}:
        path = _SRC / rel
        assert identity_name_literals(path.read_text(encoding="utf-8")), (
            f"{rel} no longer names an identity variable; drop it from _ENV_WRITERS"
        )


# Modules allowed to use the accessor's exported name constants, with why.
_CONSTANT_USERS = {
    "memory/provenance.py": "names the origin variable in one log message",
}
_EXPORTED_NAMES = _ACCESSOR_CONSTANTS | {"IDENTITY_ENV_VARS"}


def identity_constant_uses(source: str, filename: str = "<src>") -> list[tuple[int, str]]:
    """Every import or attribute use of the accessor's identity-name constants."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.ImportFrom) and node.module == "genesis.caller_context":
            hits += [(node.lineno, a.name) for a in node.names if a.name in _EXPORTED_NAMES | {"*"}]
        elif isinstance(node, ast.Attribute) and node.attr in _EXPORTED_NAMES:
            hits.append((node.lineno, node.attr))
    return hits


def test_identity_constants_are_used_only_by_the_allowlist():
    """Third net: a read keyed by an imported constant passes the other two."""
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if path == _ACCESSOR or rel in _CONSTANT_USERS:
            continue
        for line, name in identity_constant_uses(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"src/genesis/{rel}:{line} uses {name}")
    assert not offenders, (
        "Call genesis.caller_context.caller_context() instead of keying a read by "
        "its name constants:\n" + "\n".join(offenders)
    )


def test_constant_allowlist_has_no_stale_entries():
    for rel in _CONSTANT_USERS:
        assert identity_constant_uses((_SRC / rel).read_text(encoding="utf-8")), rel


def _any_net(snippet: str) -> bool:
    return bool(
        direct_identity_reads(snippet)
        or identity_name_literals(snippet)
        or identity_constant_uses(snippet)
    )


@pytest.mark.parametrize(
    "snippet",
    [
        "from genesis.caller_context import SESSION_ID_ENV\nenv = os.environ\nenv.get(SESSION_ID_ENV)",
        "from genesis.caller_context import SESSION_ID_ENV\nos.environ.copy().get(SESSION_ID_ENV)",
        "from genesis.caller_context import CC_SESSION_ENV as X\nos.environ.get(X)",
        "import genesis.caller_context as c\nimport os as _os\n_os.environ.get(c.CC_SESSION_ENV)",
        'from os import environ\nenviron.get("GENESIS_CC_SESSION")',
        "from genesis.caller_context import IDENTITY_ENV_VARS\n{k: os.environ.get(k) for k in IDENTITY_ENV_VARS}",
        'os.environ.get(key="GENESIS_CC_SESSION")',
        "from genesis.caller_context import *\nos.environ.get(CC_SESSION_ENV)",
        'b"GENESIS_CC_SESSION=1" in open("/proc/1/environ", "rb").read().split(b"\\0")',
    ],
)
def test_some_net_catches_each_evasive_shape(snippet):
    assert _any_net("import os\n" + snippet), snippet


def test_name_scan_catches_a_loop_keyed_read():
    snippet = 'import os\nfor var in ("CLAUDE_CODE_SESSION_ID", "X"):\n    os.environ.get(var)\n'
    assert direct_identity_reads(snippet) == []
    assert identity_name_literals(snippet) == [(2, "CLAUDE_CODE_SESSION_ID")]


@pytest.mark.parametrize(
    "snippet",
    [
        'os.environ.get("GENESIS_CC_SESSION")',
        'os.environ["GENESIS_SESSION_ID"]',
        'os.getenv("CLAUDE_CODE_SESSION_ID")',
        'x = os.environ.pop("GENESIS_SESSION_ORIGIN", None)',
        'os.environ.setdefault("GENESIS_SESSION_SUPERVISED", "")',
        '"CLAUDE_SESSION_ID" in os.environ',
        'NAME = "GENESIS_CC_SESSION"\nos.environ.get(NAME)',
        "from genesis.caller_context import SESSION_ORIGIN_ENV\nos.environ.get(SESSION_ORIGIN_ENV)",
        "os.environ.get(caller_context.TRANSCRIPT_ID_ENV)",
    ],
)
def test_guard_flags_each_read_shape(snippet):
    assert direct_identity_reads("import os\n" + snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'os.environ.pop("GENESIS_SESSION_ORIGIN", None)',
        'env = {}\nenv["GENESIS_CC_SESSION"] = "1"',
        'os.environ["GENESIS_CC_SESSION"] = "1"',
        'os.environ.get("GENESIS_HOME")',
    ],
)
def test_guard_ignores_clears_writes_and_other_names(snippet):
    assert direct_identity_reads("import os\n" + snippet) == [], snippet
