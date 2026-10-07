"""Drive the production J9 relevance and novelty judgments for one case.

Rubrics use the production calibration contract; runner orchestration lands later. These two
are not rubrics, so they keep a small adapter each. Production code renders
the prompt and parses the answer; qualification only reads the raw verdict,
because production clamps or fails open where a qualification must not.
"""

from __future__ import annotations

import math
import re
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

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

    async def database(self, case):
        path = Path(self.directory.name) / "case.sqlite"
        shutil.copyfile(self.template, path)
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


async def relevance(case, router) -> dict:
    score, detail, _model = await J9EvalBatchExecutor(router=router)._judge_relevance(
        case["query"], case["memory_content"]
    )
    if score is None:
        return {"prediction": None, "error": detail or "relevance_error"}
    # Inspect the answer before production's clamp can turn NaN/Infinity into
    # an apparently usable 0 or 1. Both live and recorded routers expose it.
    try:
        raw = load_json(router.calls[-1][1])
        value = raw.get("relevance") if isinstance(raw, dict) else None
        if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
            raise ValueError("non-finite or invalid relevance score")
    except (ValueError, TypeError, OverflowError, IndexError):
        return {"prediction": None, "error": "invalid raw relevance score"}
    return {"prediction": score >= RELEVANT_AT, "error": None}


async def novelty(case, router, sandbox) -> dict:
    """Run the production cross-type judgment; grade the raw target it returned."""
    db = await sandbox.database(case)
    selected_ids = []
    row_get = extractor._row_get

    def observe_row(row, key):
        value = row_get(row, key)
        # Production reads steps only while rendering the selected top-K rows.
        # Retain those actual IDs instead of reconstructing retrieval/selection.
        if key == "steps":
            identity = row_get(row, "id")
            if identity not in selected_ids:
                selected_ids.append(identity)
        return value

    try:
        new = case["new"]
        with patch.object(extractor, "_row_get", observe_row):
            await extractor._principle_is_novel(
                db,
                task_type=new["task_type"],
                new_principle=new["principle"],
                new_steps=new["steps"],
                embedder=Embedder(case),
                router=router,
            )
    finally:
        await db.close()
    if len(router.calls) != 1:
        raise Incomplete("novelty case did not reach exactly one judge request")
    messages, content = router.calls[0]
    router.candidate_ids = selected_ids
    mapping = candidate_mapping(case, messages, selected_ids)
    if case["expected_target"] is not None and case["expected_target"] not in mapping:
        raise Incomplete("reference target is not in the rendered candidate selection")
    try:
        result = {"prediction": raw_target(content, mapping), "error": None}
        return result
    except MalformedJudgment as exc:
        return {"prediction": None, "error": str(exc)}


class Probe:
    """Offline stand-in that answers 'not redundant', for ``check`` rendering only."""

    def __init__(self):
        self.calls = []

    async def route_call(self, call_site_id, messages, **_kwargs):
        content = '{"redundant_with": null}'
        self.calls.append((messages, content))
        return RoutingResult(success=True, call_site_id=call_site_id, content=content)
