"""Saved novelty verdicts through four actual, disposable production stores.

Dedicated serial process only. Synthetic extraction/scoping isolates novelty;
it does not qualify those classifiers or activate the simulated allowlist.
"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

from genesis.eval.qualification import contracts
from genesis.eval.qualification.evidence import Incomplete, canonical, load_json
from genesis.learning.procedural import (
    embedding,
    extractor,
    judge,
    operations,
    scoping,
    validation_gate,
)
from genesis.routing.types import RoutingResult


class Replay:
    def __init__(self, case, messages, content, alias, model):
        self.case, self.messages, self.content = case, messages, content
        self.alias, self.model = alias, model
        self.calls = 0
        self.scoping_calls = self.extraction_calls = 0
        self.errors = []

    async def route_call(self, call_site_id, messages, **kwargs):
        if call_site_id == extractor._NOVELTY_CALL_SITE:
            if messages != self.messages or kwargs:
                self.errors.append("storage replay changed the grading request")
                raise Incomplete(self.errors[-1])
            self.calls += 1
            content = self.content
        elif call_site_id == scoping._CALL_SITE:
            new = self.case["new"]
            classifier = scoping._CLASSIFIER_PROMPT.format(
                task_type=new["task_type"], principle=new["principle"][:600],
                steps=json.dumps(new["steps"])[:800],
            )
            if messages == [{"role": "user", "content": classifier}] and not kwargs:
                self.scoping_calls += 1
                content = '{"procedure_type":"task_procedure"}'
            else:
                summary = "\n".join([new["principle"], *new["steps"]])
                expected = extractor._PROMPT_TEMPLATE.format(summary_text=summary, outcome="success")
                if messages != [{"role": "user", "content": expected}] or kwargs:
                    self.errors.append("unexpected extraction/scoping request")
                    raise Incomplete(self.errors[-1])
                self.extraction_calls += 1
                content = json.dumps({
                    **{k: new[k] for k in ("task_type", "principle", "steps")},
                    "tools_used": ["Bash"], "context_tags": ["qualification-new"],
                })
        else:
            self.errors.append("unexpected storage replay call")
            raise Incomplete(self.errors[-1])
        return RoutingResult(success=True, call_site_id=call_site_id, content=content,
                             provider_used=self.alias, model_id=self.model)


class OtherGates:
    def __init__(self):
        self.flags, self.results = [], {}
        self._validate = validation_gate.validate_extraction
        self._classify = scoping.is_behavioral_directive
        self._novelty = extractor._principle_is_novel
        self._checked = operations.store_procedure_checked

    async def novelty(self, *args, **kwargs):
        result = await self._novelty(*args, **kwargs)
        self.results["novelty"] = {"novel": result[0], "max_similarity": result[1],
                                  "fell_open": result[3]}
        return result

    async def checked(self, *args, **kwargs):
        result = await self._checked(*args, **kwargs)
        self.results["checked_store"] = {"action": result.action}
        return result

    async def validation(self, *args, **kwargs):
        result = await self._validate(*args, **kwargs)
        self.results["validation"] = {
            "allowed": result.allowed, "flags": list(result.flags),
            "adjusted_confidence": result.adjusted_confidence,
            "first_mover": result.first_mover,
            "extraction_context": result.extraction_context,
        }
        if not result.allowed:
            self.flags.extend(result.flags or ["extraction_validation"])
        return result

    async def scoping(self, *args, **kwargs):
        result = await self._classify(*args, **kwargs)
        self.results["scoping"] = {"directive": result}
        if result:
            self.flags.append("behavioral_directive")
        return result


async def rows(db):
    """All columns, including excluded rows; active-list APIs are insufficient."""
    async with db.execute("SELECT * FROM procedural_memory ORDER BY id") as cursor:
        return {row["id"]: dict(row) for row in await cursor.fetchall()}


def insertion(before, after, stored, case, path, gates):
    if any(after.get(key) != row for key, row in before.items()):
        raise Incomplete("storage replay changed an original candidate row")
    added = set(after) - set(before)
    if added != ({stored} if stored is not None else set()) or stored in before:
        raise Incomplete("storage replay returned an existing ID or inconsistent insertion")
    if stored is None:
        return
    row, new = after[stored], case["new"]
    tools = new.get("tools_used", []) if path == "judge" else ["Bash"]
    tags = new.get("context_tags", []) if path == "judge" else ["qualification-new"]
    tools = [tools] if isinstance(tools, str) else tools
    tags = [tags] if isinstance(tags, str) else tags
    trigger = new.get("tool_trigger") if path == "judge" else None
    expected = {
        "task_type": new["task_type"], "principle": new["principle"],
        "steps": json.dumps(new["steps"]), "draft": 1, "success_count": 0,
        "activation_tier": "DORMANT" if path == "judge" else "LIBRARY",
        "principle_embedding": embedding.pack_embedding(contracts.padded(new["embedding"])),
        "confidence": 0.0 if path == "judge" else gates.results["validation"]["adjusted_confidence"],
        "deprecated": 0, "quarantined": 0,
        "person_id": None, "scenario": new.get("scenario") if path == "judge" else None,
        "tools_used": json.dumps(tools), "context_tags": json.dumps(tags),
        "tool_trigger": json.dumps(trigger) if trigger else None,
        "failure_count": 0, "invocation_count": 0, "surfaced_count": 0, "version": 1,
        "failure_modes": None, "last_used": None, "last_validated": None,
        "deprecated_reason": None, "superseded_by": None, "attempted_workarounds": None,
        "promotion_history": None,
        "first_mover": 0 if path == "judge" else int(gates.results["validation"]["first_mover"]),
        "extraction_context": None if path == "judge" else gates.results["validation"]["extraction_context"],
    }
    source = {"type": "qualification"} if path == "judge" else {
        "type": "auto_extracted", "triage_outcome": "success"
    }
    expected["source"] = json.dumps(source)
    # Exhaust the schema. Generated UUID/time are checked for their production
    # shape, not fixed values; every other column has an independent expectation.
    if set(row) != set(expected) | {"id", "created_at"} or any(
        row.get(key) != value for key, value in expected.items()
    ):
        raise Incomplete("storage replay inserted different procedure semantics")
    try:
        valid_identity = UUID(stored).version == 4
        valid_time = datetime.fromisoformat(row["created_at"]).utcoffset() == UTC.utcoffset(None)
    except (ValueError, TypeError, AttributeError):
        valid_identity = valid_time = False
    if not valid_identity or not valid_time:
        raise Incomplete("storage replay inserted invalid generated identity/time")


async def replay(case, messages, mapping, content, sandbox, alias, model):
    case, messages, mapping = load_json(canonical([case, messages, mapping]))
    contracts.candidate_mapping(case, messages, mapping)
    target = contracts.raw_target(content, mapping)
    probe = contracts.Probe()
    await contracts.novelty(case, probe, sandbox)
    selected = contracts.candidate_mapping(case, probe.calls[0][0], probe.candidate_ids)
    if probe.calls[0][0] != messages or selected != mapping:
        raise Incomplete("storage replay differs from the actual production question")
    outcomes = {}
    for promoted in (False, True):
        allowlist = dict(extractor._NOVELTY_VALIDATED_MODELS)
        if promoted:
            allowlist[alias] = model
        for path in ("judge", "extractor"):
            db = await sandbox.database(case)
            router, gates = Replay(case, messages, content, alias, model), OtherGates()
            try:
                before = await rows(db)
                with (
                    patch.dict(extractor._NOVELTY_VALIDATED_MODELS, allowlist, clear=True),
                    patch.dict(embedding._fail_open_timestamps, deepcopy(embedding._fail_open_timestamps), clear=True),
                    patch.object(judge, "get_embedding_provider", return_value=contracts.Embedder(case)),
                    patch.object(validation_gate, "validate_extraction", gates.validation),
                    patch.object(scoping, "is_behavioral_directive", gates.scoping),
                    patch.object(extractor, "_principle_is_novel", gates.novelty),
                    patch.object(operations, "store_procedure_checked", gates.checked),
                ):
                    if path == "judge":
                        stored = await judge._store_judged_procedure(
                            db, case["new"], router, source_type="qualification"
                        )
                    else:
                        stored = await extractor.extract_procedure(
                            db, summary_text="\n".join([case["new"]["principle"], *case["new"]["steps"]]),
                            outcome="success", router=router,
                            embedding_provider=contracts.Embedder(case), session_tools_count=2,
                        )
                after = await rows(db)
                insertion(before, after, stored, case, path, gates)
                expected_store = not gates.flags and (target is None or allowlist.get(alias) != model)
                if (router.errors or router.calls != (0 if "behavioral_directive" in gates.flags else 1)
                    or router.scoping_calls != 1 or router.extraction_calls != (path == "extractor")
                    or (stored is not None) != expected_store):
                    raise Incomplete("storage replay violated suppression or independent gate outcome")
                outcomes[f"{'promoted' if promoted else 'current'}_{path}"] = {
                    "stored": stored is not None, "original_candidates_preserved": True,
                    "novelty_replayed": router.calls == 1, "other_gate_rejections": gates.flags,
                    "gates": gates.results, "provider": alias, "model": model,
                    "raw_target": target, "suppression_authorized": allowlist.get(alias) == model,
                }
            finally:
                await db.close()
    return outcomes
