"""Replay a saved novelty answer through both disposable production stores."""

import json
from unittest.mock import patch

from genesis.db.crud import procedural
from genesis.eval.qualification.contracts import Embedder, raw_target
from genesis.eval.qualification.evidence import Incomplete
from genesis.learning.procedural import extractor, judge, scoping, validation_gate
from genesis.routing.types import RoutingResult


class Replay:
    def __init__(self, case, messages, content, alias, model):
        self.case, self.messages, self.content = case, messages, content
        self.alias, self.model = alias, model
        self.calls = 0

    async def route_call(self, call_site_id, messages, **kwargs):
        if call_site_id == extractor._NOVELTY_CALL_SITE:
            if messages != self.messages or kwargs:
                raise Incomplete("storage replay changed the grading request")
            self.calls += 1
            content = self.content
        elif call_site_id == "38_procedure_extraction":
            content = json.dumps(
                {
                    **{key: self.case["new"][key] for key in ("task_type", "principle", "steps")},
                    "tools_used": ["Bash"],
                    "context_tags": ["qualification-new"],
                    "procedure_type": "task_procedure",
                }
            )
        else:
            raise Incomplete("unexpected storage replay call")
        return RoutingResult(
            success=True,
            call_site_id=call_site_id,
            content=content,
            provider_used=self.alias,
            model_id=self.model,
        )


class OtherGates:
    """Observe production gate decisions without changing their semantics."""

    def __init__(self):
        self.flags = []
        self._validate = validation_gate.validate_extraction
        self._classify = scoping.is_behavioral_directive

    async def validation(self, *args, **kwargs):
        result = await self._validate(*args, **kwargs)
        if not result.allowed:
            self.flags.extend(result.flags or ["extraction_validation"])
        return result

    async def scoping(self, *args, **kwargs):
        result = await self._classify(*args, **kwargs)
        if result:
            self.flags.append("behavioral_directive")
        return result


async def replay(case, messages, mapping, content, sandbox, alias, model):
    target = raw_target(content, mapping)
    outcomes = {}
    for promoted in (False, True):
        allowlist = dict(extractor._NOVELTY_VALIDATED_MODELS)
        if promoted:
            allowlist[alias] = model
        for path in ("judge", "extractor"):
            db = await sandbox.database(case)
            router = Replay(case, messages, content, alias, model)
            other_gates = OtherGates()

            try:
                original = {r["id"] for r in await procedural.list_active(db, limit=10000)}
                with (
                    patch.dict(extractor._NOVELTY_VALIDATED_MODELS, allowlist, clear=True),
                    patch.object(judge, "get_embedding_provider", return_value=Embedder(case)),
                    patch.object(validation_gate, "validate_extraction", other_gates.validation),
                    patch.object(scoping, "is_behavioral_directive", other_gates.scoping),
                ):
                    if path == "judge":
                        stored = await judge._store_judged_procedure(
                            db, case["new"], router, source_type="qualification"
                        )
                    else:
                        stored = await extractor.extract_procedure(
                            db,
                            summary_text="\n".join(
                                [case["new"]["principle"], *case["new"]["steps"]]
                            ),
                            outcome="success",
                            router=router,
                            embedding_provider=Embedder(case),
                            session_tools_count=2,
                        )
                remaining = {r["id"] for r in await procedural.list_active(db, limit=10000)}
                expected_store = target is None or allowlist.get(alias) != model
                if other_gates.flags:
                    expected_store = False
                if (
                    router.calls != (0 if "behavioral_directive" in other_gates.flags else 1)
                    or original - remaining
                    or (stored is not None) != expected_store
                ):
                    raise Incomplete(
                        "storage replay violated suppression or candidate preservation"
                    )
                outcomes[f"{'promoted' if promoted else 'current'}_{path}"] = {
                    "stored": stored is not None,
                    "original_candidates_preserved": True,
                    "novelty_replayed": router.calls == 1,
                    "other_gate_rejections": other_gates.flags,
                }
            finally:
                await db.close()
    return outcomes
