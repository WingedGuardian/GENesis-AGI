"""CLI transport for the queue facade; receives its current API per call."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys


def main(argv: list[str] | None, api: dict) -> int:
    parser = argparse.ArgumentParser(description="Code-intel provider-leg queue")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("write")
    p.add_argument("--repo", required=True)
    p.add_argument("--tools", default="both", choices=api['VALID_TOOLS'])
    p.add_argument("--mode", default="fast", choices=api['VALID_MODES'])
    p = sub.add_parser("hash")
    p.add_argument("--repo", required=True)
    sub.add_parser("list")
    p = sub.add_parser("list-legs")
    p.add_argument("--nul", action="store_true")
    sub.add_parser("automatic-ready")
    p = sub.add_parser("import-status")
    p.add_argument("--limit", type=int, default=20)
    for name in ("claim", "claim-native"):
        p = sub.add_parser(name)
        p.add_argument("--hash", required=True)
        p.add_argument("--provider", choices=api['_legs'].PROVIDERS, required=True)
        p.add_argument("--nul", action="store_true")
    for name in ("consume", "restore", "remember-outcome", "apply-outcome"):
        p = sub.add_parser(name)
        p.add_argument("--context", required=True)
        if name == "restore":
            p.add_argument("--attempts-inc", action="store_true")
        if name == "remember-outcome":
            p.add_argument("--action", choices=api['VALID_OUTCOMES'], required=True)
    for name in ("should-escalate", "stamp-full", "mark-full-backoff"):
        p = sub.add_parser(name)
        p.add_argument("--hash", required=True)
        if name == "should-escalate":
            p.add_argument("--nul", action="store_true")
    sub.add_parser("reconcile-inflight")
    sub.add_parser("reconcile-native")
    api['_native'].add_commands(sub)
    args = parser.parse_args(argv)
    if args.cmd in api['_native'].COMMANDS:
        return api['_native'].dispatch(args, api, api['_proof'])
    machine = getattr(args, "nul", False)
    if machine and args.cmd in ("claim", "claim-native", "should-escalate"):
        api['_machine_hash'](args.hash)
    if args.cmd in ("automatic-ready", "import-status"):
        try:
            result = api['_legacy'].import_status(
                api['database_path'](),
                limit=getattr(args, "limit", 0),
                protocol_only=args.cmd == "automatic-ready",
            )
        except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
            print(
                json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=True),
                file=sys.stderr if args.cmd == "automatic-ready" else sys.stdout,
            )
            # Only the runner's observer reports busy apart; import-status keeps 2.
            busy = isinstance(exc, TimeoutError) or (
                isinstance(exc, sqlite3.OperationalError) and api['_is_busy'](exc)
            )
            return api['BUSY_STATUS'] if busy and args.cmd == "automatic-ready" else 2
        print(json.dumps(result, ensure_ascii=True))
        return api['AUTOMATIC_PROTOCOL_INCOMPLETE'] if args.cmd == "automatic-ready" else 0
    if args.cmd == "write":
        print(api['write_marker'](args.repo, args.tools, args.mode))
        return 0
    if args.cmd == "hash":
        print(api['marker_hash'](args.repo))
        return 0
    if args.cmd in ("list", "list-legs"):
        rows = api['list_markers']()
        if machine:
            api['_write_machine']([api['_pending_record'](row) for row in rows])
        elif args.cmd == "list":
            # Legacy human view only; list-legs is lossless JSON per line.
            for row in rows:
                print(
                    f"{row['hash']}\t{row['repo_path']}\t{row['provider']}\t{row['mode']}\t{row['attempts']}\t{row['age_s']}"
                )
        else:
            for row in rows:
                print(json.dumps(row, ensure_ascii=True))
        return 0
    if args.cmd in ("claim", "claim-native"):
        operation = api['claim_native'] if args.cmd == "claim-native" else api['claim']
        row = operation(args.hash, provider=args.provider)
        if row is None:
            if machine:
                api['_write_machine']([("1", "unclaimed", args.hash, args.provider)])
            return 1
        if machine:
            api['_write_machine']([api['_claim_record'](row, args.hash, args.provider,
                                         protocol=3 if args.cmd == "claim-native" else 2)])
        else:
            print(json.dumps(row, ensure_ascii=True))
        return 0
    if args.cmd == "consume":
        api['consume'](api['_cli_context'](args))
        return 0
    if args.cmd == "restore":
        print(api['restore'](api['_cli_context'](args), attempts_inc=args.attempts_inc))
        return 0
    if args.cmd == "remember-outcome":
        api['remember_outcome'](api['_cli_context'](args), args.action)
        return 0
    if args.cmd == "apply-outcome":
        state = api['apply_remembered_outcome'](api['_cli_context'](args))
        if state is None:
            return 1
        print(state)
        return 0
    if args.cmd == "should-escalate":
        due = api['should_escalate_full'](args.hash)
        if machine:
            if type(due) is not bool:
                raise ValueError("invalid machine escalation answer")
            api['_write_machine']([("1", "escalation", args.hash, "1" if due else "0")])
            return 0
        return 0 if due else 1
    if args.cmd == "stamp-full":
        api['stamp_full'](args.hash)
        return 0
    if args.cmd == "mark-full-backoff":
        api['mark_full_backoff'](args.hash)
        return 0
    if args.cmd == "reconcile-inflight":
        rows = api['repend_stale_inflight']()
        if rows:
            print(f"re-pended {len(rows)} protocol2 claims without native intent")
        return 0
    if args.cmd == "reconcile-native":
        print(json.dumps(api['reconcile_native']()))
        return 0
    return 2
