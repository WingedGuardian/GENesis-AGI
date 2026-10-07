"""Offline admission and actual production rendering, never spending approval.

Run in a dedicated serial qualification process: disposable replay temporarily
patches production globals. The runner must recompute ``check`` from current
files before credentials; a saved or caller-supplied plan is not authority.
"""

import hashlib
import inspect
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from genesis.eval.qualification import contracts, corpus, references
from genesis.eval.qualification.evidence import Incomplete, canonical, digest, load_json
from genesis.eval.qualification.pinned import ROUTE_KEYS, resolve, routing, validate_params
from genesis.eval.rubrics import list_rubrics
from genesis.eval.scorers import LLMJudgeScorer


class Capture(contracts.Probe):
    def __init__(self):
        super().__init__()
        self.parameters = []
        self.call_sites = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.parameters.append(load_json(canonical(kwargs)))
        self.call_sites.append(call_site_id)
        return await super().route_call(call_site_id, load_json(canonical(messages)), **kwargs)


async def prepare(cases, temp_root, *, reference_policy=None):
    """Render a validated subset; this primitive is NOT complete admission."""
    from genesis.eval.qualification.storage import replay

    cases, reference_policy = load_json(canonical([cases, reference_policy]))
    for name, rows in cases.items():
        corpus.validate(name, rows, reference_policy=reference_policy)
    plan = {}
    async with contracts.Sandbox(temp_root) as sandbox:
        for name, rows in cases.items():
            seen, plan[name] = set(), {}
            for case in rows:
                capture = Capture()
                if name == corpus.NOVELTY:
                    await contracts.novelty(case, capture, sandbox)
                elif name == corpus.RELEVANCE:
                    await contracts.relevance(case, capture)
                else:
                    await LLMJudgeScorer(router=capture).score_async(
                        case["actual"], case.get("expected", ""), case["scorer_config"]
                    )
                if len(capture.calls) != 1:
                    raise Incomplete(f"{name}: case must render exactly one judge request")
                messages = capture.calls[0][0]
                prompt_hash = digest(messages)
                if prompt_hash in seen:
                    raise Incomplete(f"{name}: duplicate rendered grading question")
                seen.add(prompt_hash)
                row = {
                    "messages": messages, "prompt_hash": prompt_hash,
                    "call_site_id": capture.call_sites[0],
                    "call_parameters": capture.parameters[0],
                }
                if name == corpus.NOVELTY:
                    mapping = contracts.candidate_mapping(case, messages, capture.candidate_ids)
                    target = case["expected_target"]
                    content = canonical({
                        "redundant_with": mapping.index(target) + 1 if target else None
                    }).decode()
                    row["candidate_mapping"] = mapping
                    row["storage"] = await replay(
                        case, messages, mapping, content, sandbox,
                        "qualification-probe", "synthetic",
                    )
                plan[name][case["id"]] = row
    return load_json(canonical(plan))


def parameters(alias, configured, plan, *, config=None):
    """Freeze explicit params, refusing implicit or contradictory transformations."""
    configured = load_json(canonical(validate_params(configured)))
    provider = resolve(alias, config)
    if provider.params:
        raise Incomplete("alias has implicit routing parameters forbidden in qualification")
    for name, rows in plan.items():
        body = configured[corpus.route(name)]
        for row in rows.values():
            for key, value in row["call_parameters"].items():
                if key == "chain_offset" and type(value) is int and value == 0:
                    continue
                if key not in ROUTE_KEYS or key not in body or canonical(body[key]) != canonical(value):
                    raise Incomplete(f"{name}: call-site parameter {key!r} contradicts params")
    return configured


def file_hash(path):
    """Hash disk sources with hashlib's bounded buffer, never a whole DB copy."""
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_nodes(value):
    """Composite references retain both arms and historical baseline evidence."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from source_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from source_nodes(child)


def source_evidence(cases, source_files):
    """Check the frozen original source bytes, including hashes without paths."""
    if not isinstance(source_files, dict) or not source_files:
        raise Incomplete("frozen reference source files are required")
    verified = {}
    for path, expected in source_files.items():
        if not isinstance(path, str) or not isinstance(expected, str) or len(expected) != 64:
            raise Incomplete("invalid reference source identity")
        actual = file_hash(path)
        if actual != expected:
            raise Incomplete("reference source bytes changed")
        verified[path] = actual
    for rows in cases.values():
        for case in rows:
            source = case.get("source")
            if not isinstance(source, dict):
                raise Incomplete("reference source evidence is required")
            count = 0
            for node in source_nodes(source):
                hashes = {k: v for k, v in node.items() if k == "sha256" or k.endswith("_sha256")}
                count += len(hashes)
                if any(v not in verified.values() for v in hashes.values()):
                    raise Incomplete("reference source hash is not in the frozen source files")
                for key, value in hashes.items():
                    path = node.get("path" if key == "sha256" else key.removesuffix("_sha256"))
                    # Git baselines name a historical repository-relative path;
                    # their exported immutable bytes are identified by SHA-256.
                    if path is not None and "commit" not in node and verified.get(path) != value:
                        raise Incomplete("reference source path differs from its frozen identity")
            if not count:
                raise Incomplete("reference source evidence has no frozen byte identity")
    return verified


async def check(cases, temp_root, *, reference_policy, current_policy,
                reference_approval, candidates, source_files, config=None):
    """Complete CURRENT offline evidence; conveys no consent, budget or quality."""
    from genesis.eval.qualification import storage
    from genesis.learning.procedural import extractor, judge, operations, scoping, validation_gate

    cases, policy, current, approval, candidates, sources = load_json(canonical([
        cases, reference_policy, current_policy, reference_approval, candidates, source_files
    ]))
    config = deepcopy(config or routing())
    versions = corpus.versions()
    rubric_definitions = [asdict(r) for r in list_rubrics()]
    references.validate_policy(policy, versions)
    if canonical(policy) != canonical(current):
        raise Incomplete("reference policy differs from current policy")
    for name, rows in cases.items():
        corpus.validate(name, rows, reference_policy=policy)
    issues = corpus.coverage(cases, complete=True)
    issues += references.approval_issues(cases, policy, approval, contracts=versions)
    if issues:
        raise Incomplete("; ".join(issues))
    if not isinstance(candidates, dict) or not candidates:
        raise Incomplete("explicit qualification candidates are required")
    models = {}
    for alias, configured in candidates.items():
        provider = resolve(alias, config)
        model = references.identity(provider.model_id)
        if model in {references.identity(m) for m in policy["approved_models"]}:
            raise Incomplete("qualification candidate cannot grade its own references")
        models[alias] = provider.model_id
        # Reject malformed/implicit routing settings before expensive rendering.
        parameters(alias, configured, {}, config=config)
    verified_sources = source_evidence(cases, sources)
    modules = [contracts, corpus, references, storage, extractor, judge, operations, scoping,
               validation_gate, inspect.getmodule(LLMJudgeScorer),
               inspect.getmodule(contracts.J9EvalBatchExecutor), inspect.getmodule(check),
               inspect.getmodule(resolve), inspect.getmodule(contracts.pack_embedding),
               inspect.getmodule(contracts.connect_aiosqlite_rw), contracts.procedural]
    production_sources = {
        str(Path(module.__file__).relative_to(Path(__file__).resolve().parents[4])):
            file_hash(module.__file__)
        for module in modules
    }
    plan = await prepare(cases, temp_root, reference_policy=policy)
    frozen_params = {
        alias: parameters(alias, params, plan, config=config)
        for alias, params in candidates.items()
    }
    if (source_evidence(cases, sources) != verified_sources or corpus.versions() != versions
        or [asdict(r) for r in list_rubrics()] != rubric_definitions
        or any(file_hash(module.__file__) !=
               production_sources[str(Path(module.__file__).relative_to(Path(__file__).resolve().parents[4]))]
               for module in modules)):
        raise Incomplete("source or contract changed during offline preflight")
    return {
        "complete": True, "corpus_hash": digest(cases), "policy_hash": digest(policy),
        "approval_hash": digest(approval), "versions": versions,
        "rubrics": rubric_definitions,
        "reference_sources": verified_sources, "production_sources": production_sources,
        "models": models, "parameters": frozen_params, "plan": plan,
    }
