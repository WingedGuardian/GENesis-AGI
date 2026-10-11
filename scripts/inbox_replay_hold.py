#!/usr/bin/env python3
"""Inspect or explicitly release one inbox replay hold; never dispatch work."""

from genesis.inbox.replay_hold import main

if __name__ == "__main__":
    raise SystemExit(main())
