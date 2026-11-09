"""Owned process observer for the pending-bookmark critical section."""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True)
parser.add_argument("--home", required=True)
parser.add_argument("--result", required=True)
parser.add_argument("--genesis-home", required=True)
parser.add_argument("--ready", type=int)
parser.add_argument("--release", type=int)
args = parser.parse_args()
os.environ["HOME"] = args.home
os.environ["GENESIS_HOME"] = args.genesis_home
spec = importlib.util.spec_from_file_location("owned_plan_bookmark", args.source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
pending = Path(args.home) / ".genesis/plan_bookmark_pending.json"
read_text = Path.read_text


def observed_read(path, *values, **kwargs):
    result = read_text(path, *values, **kwargs)
    if path == pending and args.ready is not None:
        os.write(args.ready, b"1")
        assert os.read(args.release, 1) == b"1"
    return result


Path.read_text = observed_read


class Manager:
    async def create_micro(self, **kwargs):
        Path(args.result).write_text(json.dumps(kwargs))
        return "owned-bookmark"


asyncio.run(module._process_plan_bookmark_pending(Manager(), lambda value: value))
