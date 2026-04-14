"""Genesis standalone server entry point.

Usage:
    python -m genesis serve                  # Start with defaults
    python -m genesis serve --port 8080      # Custom port
    python -m genesis serve --host 0.0.0.0   # Listen on all interfaces
    python -m genesis serve --no-telegram    # Skip Telegram adapter
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="genesis",
        description="Genesis standalone server",
    )
    sub = parser.add_subparsers(dest="command")

    serve_cmd = sub.add_parser("serve", help="Start Genesis standalone server")
    serve_cmd.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default: 127.0.0.1)",
    )
    serve_cmd.add_argument(
        "--port", type=int, default=5000,
        help="Port number (default: 5000)",
    )
    serve_cmd.add_argument(
        "--no-telegram", action="store_true",
        help="Skip Telegram adapter even if configured",
    )

    # Phase 6: genesis contribute <sha>
    from genesis.contribution import cli as contrib_cli
    contrib_cli.add_parser(sub)

    # Eval harness: genesis eval run/results/datasets
    from genesis.eval import cli as eval_cli
    eval_cli.add_parser(sub)

    args = parser.parse_args()

    if args.command == "serve":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )

        from genesis.util.process_lock import ProcessLock

        with ProcessLock("genesis-server"):
            asyncio.run(_serve(args))
    elif args.command in ("contribute", "eval"):
        sys.exit(args.func(args))
    else:
        parser.print_help()
        sys.exit(1)


async def _serve(args: argparse.Namespace) -> None:
    """Bootstrap and serve Genesis standalone."""
    from genesis.hosting.standalone import StandaloneAdapter

    adapter = StandaloneAdapter(
        host=args.host,
        port=args.port,
        no_telegram=args.no_telegram,
    )

    def _signal_handler():
        # Set event — serve() handles orderly shutdown after wait unblocks.
        # Mirrors bridge.py pattern: no bare create_task in signal handlers.
        adapter._shutdown_event.set()
        if adapter._runtime and adapter._runtime.awareness_loop is not None:
            adapter._runtime.awareness_loop.request_stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await adapter.bootstrap()
    await adapter.serve()

    # Orderly shutdown after serve() returns (event was set by signal handler)
    await adapter.shutdown()


if __name__ == "__main__":
    main()
