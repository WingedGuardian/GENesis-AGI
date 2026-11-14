"""Explicit full-population mock acceptance; never a paid model study.

Run in a fresh, capped interpreter with --root <new-private-directory> and
--mode bounded|billed. All approvals, labels, prices and credentials are synthetic.
"""

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import socket
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mode", choices=("bounded", "billed"), required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.absolute()
    root.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir(mode=0o700)
    for key in list(os.environ):
        if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    empty = root / "empty-secrets.env"
    empty.write_text("")
    os.environ.update(HOME=str(home), SECRETS_PATH=str(empty), LITELLM_LOCAL_MODEL_COST_MAP="True")
    network = []

    def forbidden(*_args, **_kwargs):
        network.append(True)
        raise AssertionError("Acceptance cannot use real credentials or physical network")

    socket.socket.connect = socket.socket.connect_ex = forbidden
    socket.create_connection = socket.getaddrinfo = forbidden
    import httpx

    from genesis.eval.qualification import (
        __main__,
        accounting,
        corpus,
        evidence,
        execution,
        lineage,
        pinned,
        preflight,
        run,
        study_report,
    )
    from tests.test_eval.qualification_fixtures import write_corpus
    from tests.test_eval.qualification_preflight_fixtures import admitted, params
    from tests.test_eval.qualification_transport_fixtures import Gateway

    key = "synthetic-only-key"
    os.environ[pinned.KEY_ENV] = key
    pro, flash = execution.PRO_COMPARISON
    aliases = (pro, flash) if args.mode == "bounded" else (flash,)
    cases, options = admitted(root)
    options["candidates"] = {alias: params() for alias in aliases}
    checked = asyncio.run(preflight.check(cases, root, **options))
    oracle = {}
    for name, rows in cases.items():
        for case in rows:
            prepared = checked["plan"][name][case["id"]]
            if name == corpus.NOVELTY:
                target = case["expected_target"]
                answer = {"redundant_with": prepared["candidate_mapping"].index(target) + 1 if target else None}
            else:
                answer = {"relevance" if name == corpus.RELEVANCE else "score": int(corpus.label(name, case))}
            prompt = prepared["prompt_hash"]
            assert prompt not in oracle
            oracle[prompt] = evidence.canonical(answer).decode()
    assert len(oracle) == 750
    # A complete deliberately wrong population must fail every real gate.
    # This controls the metric independently of the answer-generating oracle.
    for name, rows in cases.items():
        wrong = {}
        for case in rows:
            prediction = (None if case["expected_target"] else "candidate-a") if name == corpus.NOVELTY \
                else 1 - int(corpus.label(name, case))
            wrong[case["id"]] = run._record(name, case, {"prediction": prediction, "error": None})
        assert run.gate(name, rows, wrong)["status"] == "fail"

    class Oracle(Gateway):
        def __init__(self):
            super().__init__()
            self.posts, self.reject_next, self.rejection_prompt, self.rejections = 0, False, None, 0

        def __call__(self, request):
            assert not request.url.path.endswith("/key")
            if request.method == "POST":
                self.posts += 1
                body = json.loads(request.content)
                prompt = evidence.digest(body["messages"])
                if self.reject_next or prompt == self.rejection_prompt:
                    self.reject_next, self.rejection_prompt = False, None
                    self.rejections += 1
                    return httpx.Response(429, headers={"x-generation-id": f"reject-{self.rejections}"}, json={
                        "error": {"code": 429, "metadata": {
                            "provider_name": self.provider, "is_byok": False,
                            "provider_error_code": "engine_overloaded", "limit_source": "upstream_provider_shared_pool"}},
                        "openrouter_metadata": {"requested": body["model"], "strategy": "direct", "attempt": 1,
                            "is_byok": False, "endpoints": {"total": 1, "available": [{
                                "provider": self.provider, "model": body["model"], "selected": False}]}}})
                self.answer = oracle[prompt]
            return super().__call__(request)

    gateway = Oracle()
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    models = {}
    for alias in aliases:
        transport = {"model": checked["models"][alias], "backend_model": checked["models"][alias],
                     "receipt_model": checked["models"][alias], "params": checked["parameters"][alias],
                     "endpoint": pinned.ENDPOINT, "generation_namespace": "openrouter",
                     "receipt_provider": gateway.provider, "provider_identity_evidence": "Synthetic mapping only",
                     "credential_mode": "shared_existing", "credential_fingerprint": fingerprint}
        selected = {}
        for name, rows in cases.items():
            if alias == pro:
                selected[name] = [r["id"] for label, count in ((False, 30 if name == corpus.NOVELTY else 10),
                                                               (True, 30 if name == corpus.NOVELTY else 10))
                                  for r in [c for c in rows if bool(c["expected_target"]) == label][:count]] \
                    if name == corpus.NOVELTY else [r["id"] for label in (False, True)
                        for r in [c for c in rows if corpus.label(name, c) == label][:10]]
            else:
                selected[name] = [r["id"] for r in rows]
        binding = {"admission": run.admission_identity(checked), "transports": {alias: transport}}
        if alias == pro:
            binding["baseline_sample"] = {"purpose": "incumbent_baseline", "corpus_hash": checked["corpus_hash"],
                "case_ids": selected, "selection_evidence": "Synthetic fixed strata before responses"}
        attempts = []
        for name, rows in checked["plan"].items():
            for case_id, prepared in rows.items():
                if case_id not in selected[name]:
                    continue
                for repetition in run.REPETITIONS:
                    attempts.append({**{k: transport[k] for k in (
                        "model", "backend_model", "receipt_model", "generation_namespace", "credential_mode",
                        "credential_fingerprint", "receipt_provider", "provider_identity_evidence")},
                        "id": f"logical-{len(attempts)}", "alias": alias, "upstream": transport["params"]["upstream"],
                        "endpoint": pinned.ENDPOINT + "/chat/completions", "contract": name,
                        "version": checked["versions"][name], "case_id": case_id, "repetition": repetition,
                        "prompt_hash": prepared["prompt_hash"], "max_charge": "0.001",
                        "request_hash": evidence.digest(pinned.request_body(transport["model"], transport["params"],
                                                                           name, prepared["messages"]))})
        manifest = {"version": 2, "budget": str(accounting.total(
            accounting.currency(r["max_charge"]) for r in attempts)), "binding": binding, "attempts": attempts}
        run.validate_campaign(manifest, checked, alias, cases=cases)
        models[alias] = {"manifest": manifest}
    assert [len(models[a]["manifest"]["attempts"]) for a in aliases] == ([600, 2250] if len(aliases) == 2 else [2250])
    histories, reuse = [], {alias: {} for alias in aliases}

    async def seed():
        manifest = models[pro]["manifest"]

        async def permit(_adapter, _attempt, info):
            assert info == {"credential_mode": "shared_existing"}

        with accounting.Journal(root / "legacy", manifest) as journal:
            router = pinned.PinnedRouter(journal, alias=pro, params=checked["parameters"][pro], dispatch=True,
                credential_mode="shared_existing", dispatch_admission=permit, transport=gateway.transport)
            for index, spec in enumerate(manifest["attempts"][:110]):
                router.bind(spec["contract"], spec["repetition"], case_id=spec["case_id"])
                messages = checked["plan"][spec["contract"]][spec["case_id"]]["messages"]
                if index == 109:
                    gateway.reject_next = True
                    try:
                        await router.route_call("synthetic-seed", messages)
                    except pinned.LocalFailure:
                        pass
                    else:
                        raise AssertionError("Expected synthetic retained rejection")
                else:
                    await router.route_call("synthetic-seed", messages)
        ref = {"directory": str(root / "legacy"), "manifest_hash": evidence.digest(manifest),
               "journal_sha256": preflight.file_hash(root / "legacy/answers.jsonl"), "closure": None}
        histories.append(ref)
        with lineage.History(ref) as history:
            for spec in manifest["attempts"][:109]:
                assert lineage.eligible(history, spec["id"]) is not None
                reuse[pro][spec["id"]] = {"journal_sha256": ref["journal_sha256"], "attempt": spec["id"]}
            assert lineage.rejected(history, manifest["attempts"][109]["id"])
        seeded = {r["prompt_hash"] for r in manifest["attempts"][:110]}
        gateway.rejection_prompt = next(r["prompt_hash"] for r in manifest["attempts"] if r["prompt_hash"] not in seeded)

    if args.mode == "bounded":
        asyncio.run(seed())
    plan = {"version": 3, "protocol": study_report.PROTOCOL, "study_root": str(root / "study"),
            **study_report.source_identity(), "financial_promise": execution.PROMISE,
            "credential_mode": "shared_existing", "credential_fingerprint": fingerprint,
            "combined_allowance": "5", "price_snapshot": {"observed_at": "synthetic", "evidence": "Synthetic charges only"},
            "models": models, "histories": histories, "expense_evidence": [], "reuse": reuse,
            "max_total_replacements": 10 if histories else 0, "max_new_attempts": 2750 if histories else 2250,
            "cooldown_seconds": 0}
    consent = {"version": 2, "protocol": study_report.PROTOCOL, "plan_hash": evidence.digest(plan),
               "max_new_attempts": plan["max_new_attempts"], "author": "owner",
               "decision_evidence": "SYNTHETIC TEST ONLY, NOT ACTUAL OWNER CONSENT"}
    values = {"plan": plan, "consent": consent, "params": options["candidates"],
              "reference-policy": options["reference_policy"], "current-reference-policy": options["current_policy"],
              "reference-approval": options["reference_approval"], "source-files": options["source_files"]}
    for name, value in values.items():
        (root / (name + ".json")).write_bytes(evidence.canonical(value))
    write_corpus(root / "corpus", cases)
    common = ["--corpus", str(root / "corpus"), "--temp-root", str(root), "--study-plan", str(root / "plan.json")]
    for name in ("params", "reference-policy", "current-reference-policy", "reference-approval", "source-files"):
        common.extend(["--" + name, str(root / (name + ".json"))])
    original_init = pinned.PinnedRouter.__init__
    original_credential = pinned.credential

    def initialize(self, *positional, **keywords):
        keywords["transport"] = keywords.get("transport") or gateway.transport
        original_init(self, *positional, **keywords)

    pinned.PinnedRouter.__init__ = initialize
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = __main__.main(["study-run", *common, "--spending-approval", str(root / "consent.json")])
        (root / "run-report.json").write_text(output.getvalue())
        report = json.loads(output.getvalue())
        assert code == (2 if histories else 0), report
        expected_posts, expected_answers = (2852, 2850) if histories else (2250, 2250)
        assert gateway.posts == report["physical_history"]["dispatch"] == expected_posts
        assert gateway.receipt_reads == expected_answers and gateway.key_reads == 0
        assert report["financial"]["unresolved_attempts"] == (2 if histories else 0)
        assert report["models"][flash]["status"] == "pass"
        for alias in aliases:
            expected_replays = 180 if alias == pro else 1200
            replay = report["models"][alias]["contracts"][corpus.NOVELTY]["storage_replay"]
            assert set(replay) == {"current_judge", "current_extractor", "promoted_judge", "promoted_extractor"}
            assert all(value["replayed"] == expected_replays for value in replay.values())
        if histories:
            assert report["models"][pro]["qualification_status"] == "not_assessed"
        pinned.credential = forbidden
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            detached_code = __main__.main(["study-report", *common])
        (root / "detached-report.json").write_text(output.getvalue())
        detached = json.loads(output.getvalue())
        assert detached_code == code
        assert detached["physical_history"] == report["physical_history"]
        assert detached["financial"] == report["financial"]
        assert not network and gateway.posts == expected_posts
        (root / "acceptance.json").write_bytes(evidence.canonical({
            "status": "pass", "scope": "Synthetic integration only; no actual model quality or paid consent",
            "source": study_report.source_identity(), "mode": args.mode, "cases": 750,
            "logical_answers": expected_answers, "physical_posts": expected_posts,
            "receipt_gets": gateway.receipt_reads, "network_attempts": len(network),
            "negative_gate_contracts": len(cases), "storage_modes": 4}))
        print(json.dumps({"status": "pass", "mode": args.mode, "cases": 750, "physical_posts": expected_posts}))
    finally:
        pinned.PinnedRouter.__init__ = original_init
        pinned.credential = original_credential


if __name__ == "__main__":
    main()
