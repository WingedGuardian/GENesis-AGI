"""Drive the production J9 relevance and novelty judgments for one case.

Rubrics use the production calibration contract; runner orchestration lands later. These two
are not rubrics, so they keep a small adapter each. Production code renders
the prompt and parses the answer; qualification only reads the raw verdict,
because production clamps or fails open where a qualification must not.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from contextvars import ContextVar
from pathlib import Path

import aiosqlite

from genesis.db.connection import connect_aiosqlite_rw
from genesis.db.crud import procedural
from genesis.db.integrity import DatabaseIntegrityError
from genesis.eval.j9_batch import J9EvalBatchExecutor
from genesis.eval.qualification.evidence import Incomplete, load_json
from genesis.eval.scorers import _extract_json
from genesis.learning.procedural import extractor
from genesis.learning.procedural.embedding import EMBEDDING_DIM, pack_embedding
from genesis.routing.types import RoutingResult

# Production decides relevant at >= 0.5 (j9_aggregator._compute_memory_quality).
RELEVANT_AT = 0.5


class MalformedJudgment(Incomplete):
    """The model's answer is invalid: a model error, never a local failure."""


class Sandbox:
    """A full-schema template copied to disposable disk-backed SQLite files."""

    def __init__(self, temp_root: Path):
        self.root = temp_root
        self.directory = None

    async def __aenter__(self):
        from genesis.db.schema import create_all_tables

        self.root.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="qualification-", dir=self.root)
        self.template = Path(self.directory.name) / "template.sqlite"
        try:
            async with connect_aiosqlite_rw(self.template) as db:
                await create_all_tables(db)
        except BaseException as exc:
            self.directory.cleanup()
            if isinstance(exc, DatabaseIntegrityError):
                raise Incomplete("disposable SQLite admission failed") from exc
            raise
        return self

    async def __aexit__(self, *_exc):
        self.directory.cleanup()

    @contextlib.asynccontextmanager
    async def database(self, case):
        """One case's database, closed and deleted on exit.

        A file per call: concurrent cases on one sandbox must not share a database.
        """
        fd, name = tempfile.mkstemp(prefix="case-", suffix=".sqlite", dir=self.directory.name)
        os.close(fd)
        path = Path(name)
        try:
            shutil.copyfile(self.template, path)
            db = await self._populated(path, case)
            try:
                yield db
            finally:
                await db.close()
        finally:
            for leftover in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                leftover.unlink(missing_ok=True)

    async def _populated(self, path, case):
        try:
            db = await connect_aiosqlite_rw(path, existing_only=True)
        except DatabaseIntegrityError as exc:
            raise Incomplete("disposable SQLite admission failed") from exc
        db.row_factory = aiosqlite.Row
        try:
            for index, row in enumerate(case["existing"]):
                await procedural.create(
                    db,
                    id=row["id"],
                    task_type=row["task_type"],
                    principle=row["principle"],
                    steps=row["steps"],
                    tools_used=["Bash"],
                    context_tags=[row["id"]],
                    created_at=f"2026-01-01T00:00:{index:02d}+00:00",
                    confidence=1 - index / 1000,
                    principle_embedding=pack_embedding(padded(row["embedding"])),
                )
                # Optional flags exercise production's exclusion of such candidates.
                for flag in ("deprecated", "quarantined"):
                    if row.get(flag):
                        await procedural.update(db, row["id"], **{flag: 1})
            return db
        except BaseException:
            await db.close()
            raise


def padded(vector):
    return vector + [0.0] * (EMBEDDING_DIM - len(vector))


class Embedder:
    def __init__(self, case):
        self.vectors = {
            r["principle"]: padded(r["embedding"]) for r in [case["new"], *case["existing"]]
        }

    async def embed(self, text):
        return self.vectors[text]


def candidate_mapping(case, messages, selected_ids):
    """Candidate ids in the order the ACTUAL rendered prompt numbered them."""
    text = messages[0]["content"]
    selected = list(re.finditer(r"^  \[(\d+)\] task_type: .+$", text, re.MULTILINE))
    suffix = extractor._CROSS_TYPE_DEDUP_PROMPT.split("{candidates}", 1)[1].format()
    if (
        not selected
        or len(selected) != len(selected_ids)
        or len(set(selected_ids)) != len(selected_ids)
        or not text.endswith(suffix)
        or [int(m.group(1)) for m in selected] != list(range(1, len(selected) + 1))
    ):
        raise Incomplete("novelty candidate mapping unavailable")
    rows = {row["id"]: row for row in case["existing"]}
    signatures = set()
    for index, match in enumerate(selected):
        end = (
            selected[index + 1].start() - 1
            if index + 1 < len(selected)
            else len(text) - len(suffix)
        )
        actual = text[match.start() : end]
        row = rows.get(selected_ids[index])
        if row is None:
            raise Incomplete("novelty candidate mapping unavailable")
        steps = " | ".join(s[:160] for s in row["steps"][:6])
        signature = (
            f"task_type: {row['task_type']}\n"
            f"      principle: {row['principle']}\n      steps: {steps}"
        )
        if actual != f"  [{index + 1}] {signature}":
            raise Incomplete("novelty candidate mapping unavailable")
        if signature in signatures:
            raise Incomplete("ambiguous rendered novelty candidate identity")
        signatures.add(signature)
    return list(selected_ids)


def raw_target(content, mapping):
    match = extractor._JSON_BLOCK_RE.search(content or "")
    try:
        parsed = load_json(match.group(1) if match else content or "")
    except ValueError as exc:
        raise MalformedJudgment("invalid novelty JSON") from exc
    if not isinstance(parsed, dict) or "redundant_with" not in parsed:
        raise MalformedJudgment("missing novelty verdict")
    target = parsed["redundant_with"]
    if target is None:
        return None
    if type(target) is not int or not 1 <= target <= len(mapping):
        raise MalformedJudgment("invalid novelty target")
    return mapping[target - 1]


def raw_score(content, key: str, *, rubric: bool):
    """The unclamped number the model returned, or None when unparseable."""
    try:
        parsed = load_json(_extract_json(content) if rubric else (content or "").strip())
    except ValueError:
        return None
    value = parsed.get(key) if isinstance(parsed, dict) else None
    return value if type(value) in (int, float) else None


class RecordingRouter:
    """Case-local observation of the ordinary route_call interface."""

    def __init__(self, router):
        self.router, self.calls = router, []

    def __getattr__(self, name):
        return getattr(self.router, name)

    async def route_call(self, call_site_id, messages, **kwargs):
        result = await self.router.route_call(call_site_id, messages, **kwargs)
        self.calls.append((messages, result))
        return result


async def relevance(case, router) -> dict:
    capture = RecordingRouter(router)
    score, detail, _model = await J9EvalBatchExecutor(router=capture)._judge_relevance(
        case["query"], case["memory_content"]
    )
    # Checked FIRST, as novelty() does: production swallows a transport that
    # raises, and that is not a provider verdict to charge to the model.
    if len(capture.calls) != 1:
        raise Incomplete("relevance case did not reach exactly one judge request")
    if score is None:
        return {"prediction": None, "error": detail or "relevance_error"}
    # Production coerces and clamps; qualification requires a raw JSON number
    # in the declared domain before accepting the production decision.
    value = raw_score(capture.calls[0][1].content, "relevance", rubric=False)
    if value is None or not 0 <= value <= 1:
        return {"prediction": None, "error": "invalid raw relevance score"}
    return {"prediction": score >= RELEVANT_AT, "error": None}


# The selected-candidate list of the novelty() call running in THIS context. Each
# asyncio task runs in a copy of the context, so concurrent calls never see each
# other's list, which a per-call patch of the module global cannot promise: two
# interleaved calls would record into each other and the later restore would leave
# the earlier call's observer installed for good.
_SELECTED: ContextVar[list | None] = ContextVar("novelty_selected", default=None)
_observer = {"depth": 0, "original": None}


def _observe_row(row, key):
    value = _observer["original"](row, key)
    selected = _SELECTED.get()
    # Production reads steps only while rendering the selected top-K rows.
    # Retain those actual IDs instead of reconstructing retrieval/selection.
    if selected is not None and key == "steps":
        identity = _observer["original"](row, "id")
        if identity not in selected:
            selected.append(identity)
    return value


@contextlib.contextmanager
def _observing_rows():
    """Install ONE observer over extractor._row_get while any novelty() runs.

    Reference-counted: the first entry installs it and the last exit restores the
    original. Install and restore run without an await between check and write,
    so they cannot interleave within one event loop. It is NOT thread-safe: two
    event loops in two threads could lose the restore. Qualification runs in one
    serial, isolated process, which is the assumption this relies on.
    """
    if _observer["depth"] == 0:
        _observer["original"] = extractor._row_get
        extractor._row_get = _observe_row
    _observer["depth"] += 1
    try:
        yield
    finally:
        _observer["depth"] -= 1
        if _observer["depth"] == 0:
            extractor._row_get = _observer["original"]
            _observer["original"] = None


async def novelty(case, router, sandbox) -> dict:
    """Run the production cross-type judgment; grade the raw target it returned.

    ``Incomplete`` means the outcome cannot be attributed to the model (an unusable
    reference case, or a call production swallowed), not that the fault is local;
    a ``success=False`` routing result is the provider's and is returned as such.
    """
    capture = RecordingRouter(router)
    selected_ids = []
    token = _SELECTED.set(selected_ids)
    try:
        new = case["new"]
        async with sandbox.database(case) as db:
            with _observing_rows():
                await extractor._principle_is_novel(
                    db,
                    task_type=new["task_type"],
                    new_principle=new["principle"],
                    new_steps=new["steps"],
                    embedder=Embedder(case),
                    router=capture,
                )
    finally:
        _SELECTED.reset(token)
    if len(capture.calls) != 1:
        raise Incomplete("novelty case did not reach exactly one judge request")
    messages, response = capture.calls[0]
    # The prompt was rendered whether or not the call succeeded, so the case's
    # own validity (its mapping, its target's reachability) is settled FIRST: an
    # unusable reference case is local preparation, never a provider error.
    mapping = candidate_mapping(case, messages, selected_ids)
    if case["expected_target"] is not None and case["expected_target"] not in mapping:
        raise Incomplete("reference target is not in the rendered candidate selection")
    if not response.success:
        return {
            "prediction": None,
            "error": "novelty routing failed",
            "candidate_ids": selected_ids,
        }
    content = response.content
    # The rendered selection is returned with the result, never stored on the
    # caller's router: a valid route_call-only transport may not accept attributes.
    try:
        prediction = raw_target(content, mapping)
    except MalformedJudgment as exc:
        return {"prediction": None, "error": str(exc), "candidate_ids": selected_ids}
    return {"prediction": prediction, "error": None, "candidate_ids": selected_ids}


class Probe:
    """Offline stand-in that answers 'not redundant', for ``check`` rendering only."""

    def __init__(self):
        self.calls = []

    async def route_call(self, call_site_id, messages, **_kwargs):
        content = '{"redundant_with": null}'
        self.calls.append((messages, content))
        return RoutingResult(success=True, call_site_id=call_site_id, content=content)
