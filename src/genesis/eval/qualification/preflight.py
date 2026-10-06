"""Offline production rendering shared by checking and paid execution."""

from genesis.eval.qualification import contracts, corpus
from genesis.eval.qualification.evidence import Incomplete, digest
from genesis.eval.qualification.pinned import ROUTE_KEYS, resolve, validate_params
from genesis.eval.scorers import LLMJudgeScorer


class Capture(contracts.Probe):
    def __init__(self):
        super().__init__()
        self.parameters = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.parameters.append(kwargs)
        return await super().route_call(call_site_id, messages, **kwargs)


async def prepare(cases, temp_root, *, reference_policy=None):
    """Validate every input and render every selected production question offline."""
    for name, rows in cases.items():
        corpus.validate(name, rows, reference_policy=reference_policy)
    plan = {}
    async with contracts.Sandbox(temp_root) as sandbox:
        for name, rows in cases.items():
            seen = set()
            plan[name] = {}
            for case in rows:
                capture = Capture()
                if name == corpus.NOVELTY:
                    await contracts.novelty(case, capture, sandbox)
                    import json

                    from genesis.eval.qualification.storage import replay

                    messages = capture.calls[0][0]
                    mapping = contracts.candidate_mapping(case, messages, capture.candidate_ids)
                    target = case["expected_target"]
                    content = json.dumps(
                        {"redundant_with": mapping.index(target) + 1 if target else None}
                    )
                    await replay(
                        case,
                        messages,
                        mapping,
                        content,
                        sandbox,
                        "qualification-probe",
                        "synthetic",
                    )
                elif name == corpus.RELEVANCE:
                    await contracts.relevance(case, capture)
                else:
                    await LLMJudgeScorer(router=capture).score_async(
                        case["actual"], case.get("expected", ""), case["scorer_config"]
                    )
                if len(capture.calls) != 1:
                    raise Incomplete(f"{name}: case must render exactly one judge request")
                prompt_hash = digest(capture.calls[0][0])
                if prompt_hash in seen:
                    raise Incomplete(f"{name}: duplicate rendered grading question")
                seen.add(prompt_hash)
                plan[name][case["id"]] = {
                    "prompt_hash": prompt_hash,
                    "call_parameters": capture.parameters[0],
                }
    return plan


def parameters(alias, configured, plan, *, config=None):
    """Refuse incompatible aliases and call-site settings before reading a key."""
    configured = validate_params(configured)
    provider = resolve(alias, config)
    defaults = provider.params or {}
    # A fallback model list contradicts one-model qualification. Other body
    # transforms must be deliberately supported, rather than silently omitted.
    if defaults.get("extra_body"):
        raise Incomplete("qualification alias has unsupported extra_body transformations")
    ignored = {"timeout", "max_retries", "num_retries", "drop_params", "additional_drop_params"}
    if set(defaults) - ROUTE_KEYS - ignored - {"extra_body"}:
        raise Incomplete("qualification alias has unsupported request transformations")
    dropped = defaults.get("additional_drop_params", [])
    if not isinstance(dropped, list):
        raise Incomplete("invalid alias additional_drop_params")
    for name, rows in plan.items():
        body = configured[corpus.route(name)]
        if set(body).intersection(dropped):
            raise Incomplete("alias drops a configured qualification parameter")
        for key in ROUTE_KEYS.intersection(defaults):
            if key not in body:
                raise Incomplete(f"alias default {key!r} must be frozen in params")
        for row in rows.values():
            for key, value in row["call_parameters"].items():
                if key == "chain_offset" and value == 0:
                    continue
                if key not in ROUTE_KEYS or key not in body or body[key] != value:
                    raise Incomplete(f"{name}: call-site parameter {key!r} contradicts params")
    return configured
