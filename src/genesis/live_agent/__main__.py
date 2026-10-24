"""Opt-in local host: python -m genesis.live_agent [--socket PATH]."""

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from genesis.env import genesis_home, repo_root
from genesis.live_agent.agent import LiveAgent, owner_environ
from genesis.live_agent.host import LocalHost


def control_socket() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "genesis-live-agent" / "control.sock"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=control_socket())
    args = parser.parse_args()
    # A systemd user manager can inherit imported environment too.
    clean = owner_environ(dict(os.environ))
    os.environ.clear()
    os.environ.update(clean)
    agent = LiveAgent(
        cwd=repo_root(), state_path=genesis_home() / "live_agent" / "state.json",
        output_surface="speech",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await LocalHost(agent, args.socket).run(stop)
        # Unexpected SDK EOF must let systemd restart the persistent host.
        return 0 if stop.is_set() else 1
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(main()))
