"""Run disposable canonical proposal-operation probes without model calls."""

import argparse
import asyncio
import json
from pathlib import Path
from sqlite3 import Error as SQLiteError

from genesis.eval.ego_runtime.probe import run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-root", type=Path, required=True,
                        help="existing scratch parent for fresh disposable fixture children")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run(args.temp_root))
    except (ValueError, OSError, SQLiteError) as exc:
        print(json.dumps({"status": "incomplete", "reason": str(exc)}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
