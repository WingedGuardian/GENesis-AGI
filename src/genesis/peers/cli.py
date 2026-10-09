"""Local operator management; credential names only, never credential values."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys

from genesis.peers.registry import CAPABILITIES, MODES, PeerRegistry


async def execute(args: argparse.Namespace, registry: PeerRegistry) -> None:
    action = args.peer_action
    if action == "configure":
        await registry.configure(args.mode, args.sam_realm, args.service_url)
    elif action == "register":
        await registry.register(
            args.peer_id,
            same_owner=args.same_owner,
            daily_allowance=args.daily_allowance,
            token_name=args.token_name,
            sam_realm=args.sam_realm,
            sam_node=args.sam_node,
            principal=args.principal,
        )
    elif action == "grant":
        await registry.grant(args.peer_id, args.capability, args.decision)
    elif action == "revoke":
        await registry.revoke(args.peer_id)
    elif action == "list":
        peers = [
            {key: value for key, value in row.items() if key != "daily_allowance"}
            for row in await registry.rows()
        ]
        print(json.dumps({"settings": await registry.settings(), "peers": peers}))


def _cmd(args: argparse.Namespace) -> int:
    try:
        asyncio.run(execute(args, PeerRegistry()))
    except (ValueError, sqlite3.Error, OSError):
        # SQLite and input diagnostics can contain operator-supplied text.
        print("Peer operation refused; check arguments and migrated database.", file=sys.stderr)
        return 1
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("peers", help="Manage trusted peer relationships locally")
    actions = parser.add_subparsers(dest="peer_action", required=True)
    configure = actions.add_parser("configure", help="Select authentication explicitly")
    configure.add_argument("mode", choices=sorted(MODES))
    configure.add_argument("--sam-realm")
    configure.add_argument("--service-url")
    register = actions.add_parser("register", help="Register identity without capability grants")
    register.add_argument("peer_id")
    owners = register.add_mutually_exclusive_group(required=True)
    owners.add_argument("--same-owner", dest="same_owner", action="store_true")
    owners.add_argument("--cross-owner", dest="same_owner", action="store_false")
    register.add_argument(
        "--daily-allowance",
        type=int,
        default=1,
        help="Deprecated database compatibility value; does not impose a task quota",
    )
    register.add_argument("--token-name")
    register.add_argument("--sam-realm")
    register.add_argument("--sam-node")
    register.add_argument("--principal")
    grant = actions.add_parser("grant", help="Set one capability or resource decision")
    grant.add_argument("peer_id")
    grant.add_argument("capability", help=f"{', '.join(sorted(CAPABILITIES))}, or resource:<id>")
    grant.add_argument("decision", choices=("allow", "ask", "deny"))
    revoke = actions.add_parser("revoke", help="Immediately deactivate a relationship")
    revoke.add_argument("peer_id")
    actions.add_parser("list", help="List identities, configuration and credential names")
    parser.set_defaults(func=_cmd)
