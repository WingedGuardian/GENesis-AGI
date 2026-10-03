"""Exercise production rendering, parsing, candidate selection and storage."""

from __future__ import annotations

import json
import math
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from genesis.db.connection import connect_aiosqlite_rw
from genesis.db.crud import procedural
from genesis.db.integrity import DatabaseIntegrityError
from genesis.db.schema import create_all_tables
from genesis.eval.calibration import _validate_references
from genesis.eval.j9_batch import _RELEVANCE_PROMPT_VERSION, J9EvalBatchExecutor
from genesis.eval.qualification.evidence import Incomplete, digest, load_json
from genesis.eval.rubrics import get_rubric, list_rubrics
from genesis.eval.scorers import LLMJudgeScorer, _extract_json
from genesis.learning.procedural import extractor, judge
from genesis.learning.procedural.embedding import EMBEDDING_DIM, pack_embedding
from genesis.routing.types import RoutingResult

RELEVANCE = "j9_relevance"
NOVELTY = "procedure_novelty"


class MalformedJudgment(Incomplete):
    """A saved model response is invalid, rather than local replay failing."""

    def __init__(self, message: str, *, error: str = "MalformedJudgment"):
        super().__init__(message)
        self.error = error


def versions() -> dict[str, str]:
    return {
        **{r.name: r.version for r in list_rubrics()},
        RELEVANCE: _RELEVANCE_PROMPT_VERSION,
        NOVELTY: "source-hashed-v1",
    }


def validate_cases(cases: list[dict]):
    if not isinstance(cases, list) or not cases:
        raise Incomplete("corpus must be a nonempty list")
    grouped: dict[str, list] = {}
    seen = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or not case["id"].strip()
            or case["id"] in seen
        ):
            raise Incomplete("missing or duplicate case identity")
        seen.add(case["id"])
        contract = case.get("contract")
        if not isinstance(contract, str) or contract not in versions():
            raise Incomplete("unknown contract")
        grouped.setdefault(contract, []).append(case)
        if contract not in (RELEVANCE, NOVELTY):
            continue
        provenance = case.get("reference_provenance", {})
        if (
            not isinstance(provenance, dict)
            or provenance.get("label_source") != "human"
            or not isinstance(provenance.get("reviewer"), str)
            or not provenance["reviewer"].strip()
            or provenance.get("rubric_version") != versions()[contract]
        ):
            raise Incomplete("invalid reference provenance")
        if contract == RELEVANCE:
            if type(case.get("user_passed")) is not bool:
                raise Incomplete("relevance requires a boolean label")
            for key in ("query", "memory_content"):
                if not isinstance(case.get(key), str) or not case[key].strip():
                    raise Incomplete("missing relevance input")
        else:
            validate_novelty(case)
    for name, rows in grouped.items():
        if name not in (RELEVANCE, NOVELTY):
            _validate_references(rows, get_rubric(name))


def validate_novelty(case: dict):
    if "expected_target" not in case:
        raise Incomplete("novelty requires an explicit target or null reference")
    candidate = case.get("new")
    existing = case.get("existing")
    if not isinstance(candidate, dict) or not isinstance(existing, list) or not existing:
        raise Incomplete("novelty requires new and existing procedures")
    names, ids = set(), set()
    principles = set()
    for row in [candidate, *existing]:
        if not isinstance(row, dict):
            raise Incomplete("procedure must be a JSON object")
        for key in ("task_type", "principle"):
            if (
                not isinstance(row.get(key), str)
                or not row[key].strip()
                or "\n" in row[key]
                or "\r" in row[key]
            ):
                raise Incomplete("invalid procedure text")
        if row["task_type"] in names:
            raise Incomplete("candidate task types must be unique for ID mapping")
        names.add(row["task_type"])
        if row["principle"] in principles:
            raise Incomplete("principles must be unique for deterministic embedding mapping")
        principles.add(row["principle"])
        steps = row.get("steps")
        if (
            not isinstance(steps, list)
            or not steps
            or any(not isinstance(s, str) or not s.strip() for s in steps)
        ):
            raise Incomplete("invalid procedure steps")
        vec = row.get("embedding")
        if (
            not isinstance(vec, list)
            or not 1 <= len(vec) <= EMBEDDING_DIM
            or any(not valid_embedding_number(v) for v in vec)
            or not any(vec)
        ):
            raise Incomplete("invalid deterministic embedding")
    for row in existing:
        if not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
            raise Incomplete("invalid procedure ID")
        ids.add(row["id"])
    if case.get("expected_target") is not None and (
        not isinstance(case["expected_target"], str) or case["expected_target"] not in ids
    ):
        raise Incomplete("reference target absent from candidate population")


def valid_embedding_number(value):
    # SQLite stores float32 embeddings; finite float64 alone is insufficient.
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and abs(value) <= 3.4028234663852886e38
    except OverflowError:
        return False


def validate_raw_score(content, key, *, rubric=False, error="MalformedJudgment"):
    try:
        parsed = load_json(_extract_json(content) if rubric else content, decimal_numbers=True)
    except ValueError as exc:
        raise MalformedJudgment("invalid raw judge JSON", error=error) from exc
    value = parsed.get(key) if isinstance(parsed, dict) else None
    # Range comparison safely rejects enormous integers before any float conversion.
    if type(value) not in (int, Decimal) or not 0 <= value <= 1:
        raise MalformedJudgment("invalid raw judge score", error=error)


def coverage(cases: list[dict], *, names=None) -> list[str]:
    issues = []
    for name in versions() if names is None else names:
        rows = [c for c in cases if c["contract"] == name]
        if name == NOVELTY:
            counts = {
                label: sum((c["expected_target"] is not None) == label for c in rows)
                for label in (False, True)
            }
            if counts[False] < 300 or counts[True] < 100:
                issues.append("novelty requires 300 distinct and 100 redundant cases")
        elif len(rows) < 50 or any(
            sum(c["user_passed"] is label for c in rows) < 25 for label in (False, True)
        ):
            issues.append(f"{name} requires 50 cases and 25 per class")
    return issues


class Sandbox:
    """A full-schema template copied to disposable disk-backed SQLite files."""

    def __init__(self, temp_root: Path):
        self.root = temp_root
        self.directory = None

    async def __aenter__(self):
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

    async def __aexit__(self, *args):
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


class Recorder:
    def __init__(
        self,
        content='{"score": 1, "relevance": 1, "redundant_with": null}',
        *,
        provider="openrouter-mimo",
        model="xiaomi/mimo-v2.6-pro",
    ):
        self.content = content
        self.provider = provider
        self.model = model
        self.calls = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append({"call_site": call_site_id, "messages": messages, "kwargs": kwargs})
        return RoutingResult(
            success=True,
            call_site_id=call_site_id,
            content=self.content,
            provider_used=self.provider,
            model_id=self.model,
        )


async def exercise(case, router, sandbox=None):
    name = case["contract"]
    if name == RELEVANCE:
        score, detail, model = await J9EvalBatchExecutor(router=router)._judge_relevance(
            case["query"], case["memory_content"]
        )
        # Production clamps/coerces scores. Qualification must retain malformed
        # raw judgments as errors even if that parser returns a finite value.
        if isinstance(router, Recorder) and router.calls:
            validate_raw_score(router.content, "relevance")
        return {
            "prediction": score >= 0.5 if score is not None else None,
            "error": detail if score is None else None,
            "score": score,
            "model": model,
        }
    if name == NOVELTY:
        db = await sandbox.database(case)
        try:
            new = case["new"]
            result = await extractor._principle_is_novel(
                db,
                task_type=new["task_type"],
                new_principle=new["principle"],
                new_steps=new["steps"],
                embedder=Embedder(case),
                router=router,
            )
            return {"current_novel": result[0], "fell_open": result[3]}
        finally:
            await db.close()
    try:
        passed, score, detail = await LLMJudgeScorer(router=router).score_async(
            case["actual"], case.get("expected", ""), case["scorer_config"]
        )
    except OverflowError:
        if isinstance(router, Recorder) and router.calls:
            validate_raw_score(router.content, "score", rubric=True)
        raise
    detail = json.loads(detail)
    if not detail.get("error") and isinstance(router, Recorder) and router.calls:
        validate_raw_score(router.content, "score", rubric=True)
    return {"prediction": passed, "score": score, "error": detail.get("error"), "detail": detail}


def candidate_mapping(case, messages):
    # This is read from the ACTUAL rendered selection, not a second selector.
    names = {r["task_type"]: r["id"] for r in case["existing"]}
    selected = re.findall(r"^  \[(\d+)\] task_type: (.+)$", messages[0]["content"], re.MULTILINE)
    if not selected or [int(i) for i, _ in selected] != list(range(1, len(selected) + 1)):
        raise Incomplete("novelty candidate mapping unavailable")
    return [names[name] for _, name in selected]


def raw_target(content, mapping):
    match = extractor._JSON_BLOCK_RE.search(content or "")
    try:
        parsed = load_json(match.group(1) if match else content)
    except ValueError as exc:
        raise MalformedJudgment("invalid raw novelty JSON") from exc
    if not isinstance(parsed, dict) or "redundant_with" not in parsed:
        raise MalformedJudgment("missing raw novelty verdict")
    target = parsed["redundant_with"]
    if target is None:
        return None
    if type(target) is not int or not 1 <= target <= len(mapping):
        raise MalformedJudgment("invalid novelty target")
    return mapping[target - 1]


class StorageReplay(Recorder):
    def __init__(self, case, task, content, provider, model):
        super().__init__(content, provider=provider, model=model)
        self.case, self.task = case, task

    async def route_call(self, call_site_id, messages, **kwargs):
        if call_site_id == extractor._NOVELTY_CALL_SITE:
            if (
                call_site_id != self.task["call_site"]
                or messages != self.task["messages"]
                or kwargs != self.task["kwargs"]
            ):
                raise Incomplete("storage replay changed the frozen request")
            return await super().route_call(call_site_id, messages, **kwargs)
        # Offline extraction/scoping stubs; no model calls beyond the captured verdict.
        if call_site_id == "38_procedure_extraction":
            content = {
                **self.case["new"],
                "tools_used": ["Bash"],
                "context_tags": ["qualification-new"],
                "procedure_type": "task_procedure",
            }
            return RoutingResult(
                success=True, call_site_id=call_site_id, content=json.dumps(content)
            )
        raise Incomplete("unexpected storage replay call")


async def replay_storage(case, task, content, sandbox, provider, model):
    target = raw_target(content, task["candidate_ids"])
    outcomes = {}
    for promoted in (False, True):
        for path in ("judge", "extractor"):
            db = await sandbox.database(case)
            router = StorageReplay(case, task, content, provider, model)
            try:
                # Strictly local, sequential, restored on exit; no live router or DB.
                # Only the allowlist is changed for the hypothetical promoted arm.
                allowlist = {**extractor._NOVELTY_VALIDATED_MODELS}
                if promoted:
                    allowlist[provider] = model
                with (
                    patch.dict(extractor._NOVELTY_VALIDATED_MODELS, allowlist, clear=True),
                    patch.object(judge, "get_embedding_provider", return_value=Embedder(case)),
                ):
                    if path == "judge":
                        stored = await judge._store_judged_procedure(
                            db, case["new"], router, source_type="qualification"
                        )
                    else:
                        stored = await extractor.extract_procedure(
                            db,
                            summary_text="Synthetic extraction stub",
                            outcome="success",
                            router=router,
                            embedding_provider=Embedder(case),
                            session_tools_count=2,
                        )
                if len(router.calls) != 1:
                    raise Incomplete("storage path did not replay exactly one novelty judgment")
                rows = await procedural.list_active(db, limit=501)
                outcomes[f"{'promoted' if promoted else 'current'}_{path}"] = {
                    "stored": stored is not None,
                    "remaining_ids": [r["id"] for r in rows if r["id"] in task["candidate_ids"]],
                }
                expected_store = target is None if promoted else True
                if (stored is not None) != expected_store or set(
                    outcomes[f"{'promoted' if promoted else 'current'}_{path}"]["remaining_ids"]
                ) != set(task["candidate_ids"]):
                    raise Incomplete(
                        "storage replay violated suppression or candidate preservation"
                    )
            finally:
                await db.close()
    return {"prediction": target, "error": None, "storage": outcomes}


async def render(case, sandbox, *, provider="openrouter-mimo", model="xiaomi/mimo-v2.6-pro"):
    recorder = Recorder(provider=provider, model=model)
    await exercise(case, recorder, sandbox)
    if len(recorder.calls) != 1:
        raise Incomplete("case must reach exactly one real contract request")
    call = recorder.calls[0]
    call["prompt_hash"] = digest(call["messages"])
    if case["contract"] == NOVELTY:
        call["candidate_ids"] = candidate_mapping(case, call["messages"])
        if (
            case["expected_target"] is not None
            and case["expected_target"] not in call["candidate_ids"]
        ):
            raise Incomplete("reference target not in rendered candidate selection")
        # Establish fixture mechanics before any paid attempt. These controls
        # are not reference labels or evidence of a model's judgment quality.
        for target in (None, 1):
            await replay_storage(
                case, call, json.dumps({"redundant_with": target}), sandbox, provider, model
            )
    return call
