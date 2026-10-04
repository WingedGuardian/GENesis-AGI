"""Model qualification by routing alias: python -m genesis.eval.qualification --help.

``check`` and ``report`` never send a request. ``run`` sends only the requests
whose answers the campaign does not already hold, with a dedicated key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("check", "run", "report"):
        command = commands.add_parser(name)
        command.add_argument("--corpus", type=Path, required=True, help="one JSONL per contract")
        command.add_argument(
            "--temp-root", type=Path, required=True, help="disk-backed disposable SQLite directory"
        )
        if name == "check":
            continue
        command.add_argument("--alias", required=True, help="OpenRouter routing alias")
        command.add_argument("--campaign", type=Path, required=True, help="private 0700 directory")
        command.add_argument(
            "--params", type=Path, required=True, help="JSON object keyed by alias"
        )
        if name == "report":
            command.add_argument("--against", help="second alias for a paired report")
    return result


def _params(path: Path, alias: str):
    from genesis.eval.qualification.evidence import Incomplete, load_json

    data = load_json(path.read_bytes())
    if not isinstance(data, dict) or alias not in data:
        raise Incomplete(f"params file has no entry for {alias!r}")
    return data[alias]


async def run(args) -> dict:
    from genesis.eval.qualification import contracts, corpus
    from genesis.eval.qualification import run as qualification
    from genesis.eval.qualification.evidence import Campaign, Incomplete
    from genesis.eval.qualification.pinned import resolve, validate_params

    cases = corpus.load(args.corpus)
    if args.command == "check":
        async with contracts.Sandbox(args.temp_root) as sandbox:
            for case in cases.get(corpus.NOVELTY, []):
                await contracts.novelty(case, contracts.Probe(), sandbox)
        issues = corpus.coverage(cases, complete=True)
        return {
            "status": "incomplete" if issues else "pass",
            "corpus": corpus.counts(cases),
            "coverage_issues": issues,
        }
    if args.command == "run" and corpus.coverage(cases):
        raise Incomplete("corpus coverage is incomplete; run `check` and finish labelling")
    aliases = [args.alias] + ([args.against] if getattr(args, "against", None) else [])
    if len(set(aliases)) != len(aliases):
        raise Incomplete("a paired report needs two different aliases")
    for alias in aliases:
        resolve(alias)
    configured = {alias: validate_params(_params(args.params, alias)) for alias in aliases}
    with Campaign(args.campaign) as campaign:
        reports, records = {}, {}
        for alias in aliases:
            reports[alias], records[alias] = await qualification.qualify(
                alias,
                cases,
                configured[alias],
                campaign,
                args.temp_root,
                dispatch=args.command == "run",
            )
    if len(aliases) == 1:
        return reports[args.alias]
    return {
        "status": qualification.worst(
            (r["status"] for r in reports.values()), qualification.OVERALL
        ),
        "models": reports,
        "paired": qualification.pair(cases, *(records[a] for a in aliases)),
        "eligible_by_route": {
            route: [a for a in aliases if reports[a]["routes"][route] == "pass"]
            for route in ("judge", "novelty")
        },
        "limitations": [
            "Agreement between models is not truth; each is graded against owner labels.",
            "This report selects no winner and promotes no route.",
        ],
    }


def main(argv=None):
    from genesis.eval.qualification.evidence import Incomplete

    args = parser().parse_args(argv)
    # Pin LiteLLM's bundled price map BEFORE it is imported: its default import
    # fetches a public map over HTTP, and this CLI never uses SDK prices.
    previous = os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    try:
        result = asyncio.run(run(args))
    except (ValueError, OSError) as exc:
        from genesis.eval.qualification.pinned import safe_text

        # Error text can echo a prompt or a credential: keep the type and a redacted reason.
        reason = safe_text(str(exc)) if isinstance(exc, Incomplete) else None
        result = {"status": "incomplete", "error": type(exc).__name__, "reason": reason}
    finally:
        if previous is None:
            os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
        else:
            os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = previous
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {"pass": 0, "fail": 1}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
