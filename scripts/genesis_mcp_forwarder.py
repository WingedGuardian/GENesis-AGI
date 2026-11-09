#!/usr/bin/env python3
"""Explicit shared-only stdio forwarder; rollout/local-union activation is separate."""

import argparse
import logging
import os
import sys
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None):
    from genesis.mcp.shared_client._diagnostics import SafeDiagnostics

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, choices=("health", "memory", "outreach", "recon"))
    parser.add_argument("--url", help="Must match this service's configured loopback URL")
    args = parser.parse_args(argv)
    handler = logging.StreamHandler()
    handler.addFilter(SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    try:
        from genesis.mcp.shared_http import require_qualified_stack

        require_qualified_stack()
        from genesis.caller_context import caller_context_from_env
        from genesis.caller_credentials import forwarder_headers
        from genesis.mcp.shared_client import serve_stdio
        from genesis.mcp.shared_runner import require_source_checkout
        from genesis.mcp.shared_settings import load_shared_settings

        require_source_checkout(ROOT)
        settings = load_shared_settings(ROOT, args.server, os.environ)
        if args.url is not None and args.url != settings.http.url:
            raise ValueError("Forwarder URL differs from configuration")

        def headers():
            return forwarder_headers(caller_context_from_env())

        headers()  # Missing credentials/unsupported callers fail before claiming stdio.
        import anyio

        anyio.run(partial(serve_stdio, settings, headers))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        logging.error("Shared MCP forwarder unavailable (%s)", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
