"""Offline by default: python -m genesis.eval.qualification --help."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from genesis.eval.qualification.evidence import Incomplete, Journal, load_json


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("prepare", "dry-run", "execute", "reconcile", "report"):
        command = commands.add_parser(name)
        command.add_argument("campaign", type=Path, help="private campaign directory (0700)")
        if name == "prepare":
            command.add_argument("--spec", type=Path, required=True)
        if name in ("prepare", "execute"):
            command.add_argument(
                "--temp-root",
                type=Path,
                required=True,
                help="explicit disk-backed disposable SQLite directory",
            )
    return result


async def run(args):
    from genesis.eval.qualification.manifest import prepare
    from genesis.eval.qualification.runner import execute, report
    from genesis.eval.qualification.transport import qualification_key, reconcile, safe_text

    manifest = None
    if args.command == "prepare":
        manifest = await prepare(load_json(args.spec.read_bytes()), temp_root=args.temp_root)
    with Journal(args.campaign) as journal:
        if manifest is not None:
            journal.initialize(manifest)
        failure = None
        try:
            if args.command == "execute":
                qualification_key()
                await execute(journal, temp_root=args.temp_root)
            elif args.command == "reconcile":
                await reconcile(journal)
        except (Incomplete, OSError, ValueError, KeyError, TypeError) as exc:
            if journal._file is None:
                raise  # In-memory state may not reflect a failed durable write.
            failure = {"error": type(exc).__name__}
            if isinstance(exc, Incomplete):
                failure["reason"] = safe_text(str(exc))
        result = report(journal)
        if failure:
            result.update(status="incomplete", execution_error=failure)
        if args.command in ("prepare", "dry-run"):
            result.pop("attempts")
        return result


def main(argv=None):
    args = parser().parse_args(argv)
    # The isolated CLI never needs SDK-estimated prices; verified provider
    # evidence owns billing. Pin the bundled map BEFORE importing LiteLLM,
    # whose default import otherwise fetches a public map over HTTP.
    previous_map = os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    try:
        result = asyncio.run(run(args))
    except (Incomplete, OSError, ValueError, KeyError, TypeError) as exc:
        # Error text may contain private prompts or credentials. Keep it local
        # and machine-readable without echoing arbitrary exception contents.
        result = {"status": "incomplete", "error": type(exc).__name__}
        if isinstance(exc, Incomplete):
            from genesis.eval.qualification.transport import safe_text

            result["reason"] = safe_text(str(exc))
    finally:
        if previous_map is None:
            os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
        else:
            os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = previous_map
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "pass" else 1 if result["status"] == "fail" else 2


if __name__ == "__main__":
    raise SystemExit(main())
