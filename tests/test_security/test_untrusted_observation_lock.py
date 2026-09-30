"""An untrusted session may not forge a privileged observation.

``observation_write`` runs inside sessions that read untrusted content (the inbox
judge, the research executor). Several raw observation readers pick rows by type
or source with no origin filter, including the ego's context builders, so a row
such an untrusted session wrote as ``escalation_to_user_ego`` or under a Genesis
pipeline's source would be read as Genesis's own. The write side refuses those.

The guard at the bottom derives every type/source literal the raw readers key on,
so a new reader keyed on a new literal fails here until it is classified.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.memory import provenance
from genesis.memory.provenance import (
    RESERVED_OBSERVATION_TYPES,
    untrusted_observation_refusal,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "genesis"

# Types readers key on that an untrusted session MAY write, and why.
_WRITABLE_READ_TYPES = {
    "user_signal": "the inbox judge's sanctioned output; the reflection reader filters on origin",
    "architecture_insight": "an inbox finding; its reflection reader filters on origin",
    "interaction_theme": "its reflection reader filters on origin; others treat it as context",
    "finding": "recon/campaign findings; readers count them or also key on a reserved source",
    "awareness_tick": "only ever EXCLUDED by a reader (NOT IN), so writing it hides nothing",
}
# Sources readers key on that an untrusted session may use, and why.
_WRITABLE_READ_SOURCES = {
    "inbox_evaluation": "the inbox judge's own source; its reflection reader filters on origin",
}


async def _write(monkeypatch, origin: str | None, *, source: str, type_: str):
    import genesis.mcp.memory_mcp as mod
    from genesis.mcp.memory_mcp import mcp

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables

        await create_all_tables(db)
        await db.commit()
        saved = mod._store, mod._db, mod._retriever
        try:
            mod._store, mod._db, mod._retriever = MagicMock(), db, MagicMock()
            if origin is None:
                monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)
            else:
                monkeypatch.setenv("GENESIS_SESSION_ORIGIN", origin)
            tools = await mcp.get_tools()
            try:
                obs_id = await tools["observation_write"].fn(content="x", source=source, type=type_)
                error = None
            except ValueError as exc:
                obs_id, error = None, str(exc)
            cur = await db.execute("SELECT COUNT(*) FROM observations")
            (count,) = await cur.fetchone()
            return obs_id, error, count
        finally:
            mod._store, mod._db, mod._retriever = saved


@pytest.mark.asyncio
@pytest.mark.parametrize("type_", sorted(RESERVED_OBSERVATION_TYPES))
async def test_an_untrusted_session_cannot_write_a_reserved_type(monkeypatch, type_):
    obs_id, error, count = await _write(
        monkeypatch, "external_untrusted", source="inbox_evaluation", type_=type_
    )
    assert obs_id is None and "reserved" in error and count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "genesis_version",
        "ego_dispatch",
        "recon",
        "conversation_intent",
        "retrospective",
        "ego_domain_redirect:x",
        "intake:github_landscape",
        "  Genesis_Version  ",
    ],
)
async def test_an_untrusted_session_cannot_claim_a_pipeline_source(monkeypatch, source):
    obs_id, error, count = await _write(
        monkeypatch, "external_untrusted", source=source, type_="user_signal"
    )
    assert obs_id is None and "Genesis pipeline" in error and count == 0


@pytest.mark.asyncio
async def test_the_inbox_judges_own_signal_is_still_written(monkeypatch):
    obs_id, error, count = await _write(
        monkeypatch, "external_untrusted", source="inbox_evaluation", type_="user_signal"
    )
    assert error is None and obs_id and count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "first_party", "owner"])
async def test_a_trusted_session_is_not_restricted(monkeypatch, origin):
    obs_id, error, count = await _write(
        monkeypatch, origin, source="genesis_version", type_="genesis_update_available"
    )
    assert error is None and obs_id and count == 1


def test_the_refusal_is_none_for_ordinary_writes():
    assert untrusted_observation_refusal("inbox_evaluation", "user_signal") is None
    assert untrusted_observation_refusal("marketing_campaign", "finding") is None


# ─── derived guard: every literal a reader keys on is classified ─────────────
#
# Two reader shapes are scanned. Raw SQL near ``FROM observations`` (a quoted
# literal after type/source), and every call into the observations CRUD module:
# its arguments are mapped to parameter names through the REAL signatures, so a
# positional type (``exists_recent_by_type(db, "x")``) is seen as well as a
# keyword one. A literal held in a variable is not seen; that limit is stated.

_LITERAL_RE = re.compile(
    r"\b(type|source)\s*(?:=|!=|IN|NOT IN)\s*\(?\s*('[^']*'(?:\s*,\s*'[^']*')*)"
)
_FIELD_OF_PARAM = {"type": "type", "types": "type", "obs_type": "type", "obs_types": "type",
                   "type_": "type", "source": "source", "sources": "source"}


def _reader_literals() -> dict[str, set[str]]:
    import ast
    import inspect

    import genesis.db.crud.observations as crud

    sigs = {name: list(inspect.signature(fn).parameters) for name, fn in vars(crud).items()
            if inspect.isfunction(fn) and fn.__module__ == crud.__name__}
    found: dict[str, set[str]] = {"type": set(), "source": set()}
    for path in _SRC.rglob("*.py"):
        if any("migrations" in part for part in path.parts):  # one-shot upgrades
            continue
        if path == Path(crud.__file__):
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"FROM\s+observations\b", text):
            window = text[m.start() : m.start() + 700]
            for field, values in _LITERAL_RE.findall(window):
                found[field].update(v.strip("'") for v in re.findall(r"'[^']*'", values))
        for node in ast.walk(ast.parse(text)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            params = sigs.get(node.func.attr)
            if params is None or node.func.attr in ("create", "upsert"):
                continue
            pairs = [(params[i], arg) for i, arg in enumerate(node.args) if i < len(params)]
            pairs += [(kw.arg, kw.value) for kw in node.keywords]
            for name, value in pairs:
                field = _FIELD_OF_PARAM.get(name or "")
                if field:
                    found[field].update(
                        c.value for c in ast.walk(value)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str))
    return found


def test_the_guard_sees_both_reader_shapes():
    """Guard-the-guard: literals from raw SQL AND from CRUD calls (keyword and
    positional) must all be found, or the classification below proves nothing."""
    lit = _reader_literals()
    assert {"escalation_to_user_ego", "genesis_update_available"} <= lit["type"]  # raw SQL
    assert {"pending_question", "self_assessment", "light_escalation_pending"} <= lit["type"]  # query()
    assert {"skill_proposal", "reflection_summary"} <= lit["type"]  # exists_recent_by_type
    assert {"genesis_version", "ego_dispatch", "recon", "routing"} <= lit["source"]


def test_every_type_a_raw_reader_keys_on_is_classified():
    unclassified = (
        _reader_literals()["type"] - RESERVED_OBSERVATION_TYPES - set(_WRITABLE_READ_TYPES)
    )
    assert not unclassified, (
        f"raw observation readers key on {sorted(unclassified)}: add each to "
        "RESERVED_OBSERVATION_TYPES, or to _WRITABLE_READ_TYPES here with a reason"
    )


def test_every_source_a_raw_reader_keys_on_is_refused_or_classified():
    unrefused = {
        s
        for s in _reader_literals()["source"]
        if untrusted_observation_refusal(s, "user_signal") is None
    } - set(_WRITABLE_READ_SOURCES)
    assert not unrefused, (
        f"raw observation readers key on sources {sorted(unrefused)} that an untrusted "
        "session could claim: register them in provenance, or classify them here"
    )


def test_reserved_types_and_the_provenance_registries_are_disjoint_from_writable():
    assert not (RESERVED_OBSERVATION_TYPES & set(_WRITABLE_READ_TYPES))
    assert "inbox_evaluation" not in provenance._FIRST_PARTY_OBS_SOURCES
