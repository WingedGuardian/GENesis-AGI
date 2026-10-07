"""Offline qualification admission and private reports; paid execution is unavailable."""

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("check", "run", "report"):
        command = commands.add_parser(name)
        for flag in ("corpus", "temp-root", "reference-policy", "current-reference-policy",
                     "reference-approval", "source-files", "params"):
            command.add_argument("--" + flag, type=Path, required=flag in ("corpus", "temp-root"))
        command.add_argument("--alias", required=name != "check")
        if name != "check":
            command.add_argument("--campaign", type=Path, required=True)
        if name == "report":
            command.add_argument("--against")
            command.add_argument("--against-campaign", type=Path)
    review = commands.add_parser("review-references")
    review.add_argument("--spec", type=Path, required=True)
    return result


async def run(args):
    from genesis.eval.qualification import corpus, preflight, references
    from genesis.eval.qualification import run as qualification
    from genesis.eval.qualification.evidence import Incomplete, load_json

    def read(name):
        path = getattr(args, name, None)
        return load_json(path.read_bytes()) if path else None

    if args.command == "review-references":
        reviewed = references.review(read("spec"), corpus.versions())
        # Reference IDs and issue text can contain private user data.
        return {"status": reviewed["status"], "counts": reviewed["counts"],
                "scope": "reference admission only"}
    policy = read("reference_policy")
    cases = corpus.load(args.corpus, reference_policy=policy)
    issues = corpus.coverage(cases, complete=True)
    if issues:
        if args.command == "check":
            await preflight.prepare(cases, args.temp_root, reference_policy=policy)
        return {"status": "incomplete", "reason": "coverage_incomplete",
                "corpus": corpus.counts(cases), "coverage_issues": issues}
    configured = read("params")
    if not isinstance(configured, dict) or not configured:
        raise Incomplete("admission_inputs_missing")
    options = {
        "reference_policy": policy, "current_reference_policy": read("current_reference_policy"),
        "reference_approval": read("reference_approval"), "source_files": read("source_files"),
    }
    if args.command in ("check", "run"):
        aliases = [args.alias] if args.alias else list(configured)
        checked = await preflight.check(
            cases, args.temp_root, reference_policy=policy,
            current_policy=options["current_reference_policy"],
            reference_approval=options["reference_approval"], source_files=options["source_files"],
            candidates={alias: configured[alias] for alias in aliases},
        )
        if args.command == "run":
            qualification._paid_eligibility(checked)
        return {"status": "pass", "scope": "offline admission only", "corpus": corpus.counts(cases)}
    against = args.against
    if bool(against) != bool(args.against_campaign) or against == args.alias:
        raise Incomplete("paired_campaigns_invalid")
    paths = [args.campaign] + ([args.against_campaign] if against else [])
    if len(paths) == 2 and paths[0].resolve() == paths[1].resolve():
        raise Incomplete("paired_campaigns_invalid")
    reports, records = {}, {}
    aliases = [args.alias] + ([against] if against else [])
    for alias, path in zip(aliases, paths, strict=True):
        reports[alias], records[alias] = await qualification.qualify(
            alias, cases, configured[alias], path, args.temp_root, **options
        )
    if not against:
        return reports[args.alias]
    if reports[args.alias]["admission"] != reports[against]["admission"]:
        raise Incomplete("paired_admission_changed")
    return {
        "status": qualification.worst((r["status"] for r in reports.values()), qualification.OVERALL),
        "models": reports, "paired": qualification.pair(cases, *(records[a] for a in aliases)),
    }


def main(argv=None):
    args = parser().parse_args(argv)
    previous = os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    try:
        # Dedicated serial CLI: SDK/scorer warnings and logs may echo private answers.
        # Only the fixed aggregate result is public; campaign evidence stays private.
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = asyncio.run(run(args))
    except Exception as exc:
        allowed = {"financial_eligibility_unavailable", "admission_inputs_missing",
                   "paired_campaigns_invalid", "paired_admission_changed"}
        reason = str(exc) if str(exc) in allowed else "admission_or_processing_failed"
        result = {"status": "incomplete", "reason": reason}
    finally:
        if previous is None:
            os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
        else:
            os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = previous
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {"pass": 0, "fail": 1}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
