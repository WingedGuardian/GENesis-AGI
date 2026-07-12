"""CLI entry points for the infrastructure body schema.

``--claude-md-block`` is invoked by ``scripts/update.sh`` after a deploy to
re-render the user-level CLAUDE.md block from the LAST collected profile —
no collection, no runtime, no LLM (deploys must stay fast and dependency-free).

``--refresh`` runs a full facts collection without a runtime (no annotations,
no drift observations — those need the server's router/db). Useful on fresh
installs and for manual inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    """Dispatch --claude-md-block / --refresh (see module docstring)."""
    parser = argparse.ArgumentParser(prog="python -m genesis.infra_profile")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--claude-md-block",
        action="store_true",
        help="re-render the CLAUDE.md container-specs block from the stored profile",
    )
    group.add_argument(
        "--refresh",
        action="store_true",
        help="collect facts and render the document (no runtime: facts only)",
    )
    args = parser.parse_args()

    if args.claude_md_block:
        from genesis.infra_profile import claude_md, store

        profile = store.load_profile()
        changed = claude_md.update_block(profile, ignore_update_gate=True)
        print(json.dumps({"ok": True, "changed": changed}))
        return 0

    from genesis.infra_profile import service

    profile = asyncio.run(service.refresh("cli", force=True))
    sections = profile.get("sections", {})
    print(
        json.dumps(
            {
                "ok": True,
                "sections": {name: section.get("status") for name, section in sections.items()},
            },
            indent=2,
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
