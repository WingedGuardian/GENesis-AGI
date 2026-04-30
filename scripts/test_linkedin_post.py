#!/usr/bin/env python3
"""End-to-end LinkedIn post verification script.

Verifies that the distribution module (PR #182) can actually publish to
and delete from LinkedIn via the Composio SDK.

Usage:
    python scripts/test_linkedin_post.py
    python scripts/test_linkedin_post.py --delete <post_id>
    python scripts/test_linkedin_post.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def load_secrets() -> None:
    """Load secrets.env into the process environment."""
    secrets_path = Path.home() / "genesis" / "secrets.env"
    if not secrets_path.exists():
        print(f"[ERROR] secrets.env not found at {secrets_path}")
        sys.exit(1)

    with open(secrets_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            # Strip inline comments and quotes
            value = value.split("#")[0].strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)

    print(f"[OK] Loaded secrets from {secrets_path}")


TEST_CONTENT = (
    "Most people think the hard part of building an autonomous agent is the AI. "
    "It's not. It's the infrastructure underneath it -- the memory, the learning loop, "
    "the reflection cycles. Get those wrong and the AI has nothing real to think with."
)


async def run_publish(dry_run: bool = False) -> str | None:
    """Publish a test post and return the post_id."""
    # Import after secrets are loaded
    from genesis.distribution.config import load_distribution_config
    from genesis.distribution.linkedin import LinkedInDistributor

    config = load_distribution_config()
    li_config = config.linkedin

    print(f"\n--- LinkedIn Config ---")
    print(f"  connected_account_id : {li_config.connected_account_id or '(empty)'}")
    print(f"  author_urn           : {li_config.author_urn or '(empty)'}")
    print(f"  user_id              : {li_config.user_id}")
    print(f"  COMPOSIO_API_KEY     : {'set' if os.environ.get('COMPOSIO_API_KEY') else 'MISSING'}")

    distributor = LinkedInDistributor(config=li_config)

    if not distributor.available:
        print("\n[FAIL] Distributor not available. Missing config or COMPOSIO_API_KEY.")
        print("  Check ~/.genesis/config/distribution.yaml and secrets.env")
        sys.exit(1)

    print(f"\n[OK] Distributor initialized. Platform: {distributor.platform}")

    if dry_run:
        print(f"\n[DRY RUN] Would publish with CONNECTIONS visibility:")
        print(f"  Content: {TEST_CONTENT}")
        return None

    print(f"\n[...] Publishing to LinkedIn (visibility=CONNECTIONS)...")
    result = await distributor.publish(TEST_CONTENT, visibility="CONNECTIONS")

    print(f"\n--- PostResult ---")
    print(f"  status   : {result.status}")
    print(f"  post_id  : {result.post_id}")
    print(f"  url      : {result.url}")
    print(f"  error    : {result.error}")

    if result.status == "published":
        print(f"\n[SUCCESS] Post published.")
        print(f"  URL: {result.url}")
        print(f"  To delete: python scripts/test_linkedin_post.py --delete {result.post_id}")
        return result.post_id
    else:
        print(f"\n[FAIL] Publish failed: {result.error}")
        return None


async def run_delete(post_id: str) -> None:
    """Delete a post by ID."""
    from genesis.distribution.config import load_distribution_config
    from genesis.distribution.linkedin import LinkedInDistributor

    config = load_distribution_config()
    distributor = LinkedInDistributor(config=config.linkedin)

    if not distributor.available:
        print("[FAIL] Distributor not available.")
        sys.exit(1)

    print(f"[...] Deleting post {post_id}...")
    ok = await distributor.delete(post_id)

    if ok:
        print(f"[SUCCESS] Post {post_id} deleted.")
    else:
        print(f"[FAIL] Could not delete post {post_id}. May need to delete manually.")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test LinkedIn distribution end-to-end")
    parser.add_argument("--delete", metavar="POST_ID", help="Delete a post by ID instead of publishing")
    parser.add_argument("--dry-run", action="store_true", help="Show config and exit without posting")
    args = parser.parse_args()

    # Add venv site-packages to path if needed
    venv_site = Path.home() / "genesis" / ".venv" / "lib" / "python3.12" / "site-packages"
    if venv_site.exists() and str(venv_site) not in sys.path:
        sys.path.insert(0, str(venv_site))

    # Add genesis src to path
    src_path = Path.home() / "genesis" / "src"
    if src_path.exists() and str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

    load_secrets()

    if args.delete:
        asyncio.run(run_delete(args.delete))
    else:
        asyncio.run(run_publish(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
