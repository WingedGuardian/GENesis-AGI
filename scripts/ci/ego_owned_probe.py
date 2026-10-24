"""Trusted marker/fault wrapper for contained C1 recovery qualification only."""

import argparse
import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import ego_owned_loop as owned
import headlong_native_bridge as bridge
from headlong_controls import preflight
from headlong_prompt_references import load_config

CRASHES = ("before_intake", "after_intake", "prepared", "trigger", "config", "bound",
           "native", "claimed", "effect", "receipt", "collected", "committed")
FAULTS = ("db_close", "driver_open", "driver_short", "driver_close", "rename", "stdout", "stdout_flush")


def control(root):
    value = owned.load_json((root / "control.json").read_text())
    if (set(value) != {"root", "source_pin", "sources", "mode", "stage"}
            or value["root"] != str(root) or value["source_pin"] != owned.source_pin()
            or value["sources"] != owned.source_hashes()
            or value["mode"] not in {"crash", "late", "pass"}
            or value["stage"] not in (CRASHES if value["mode"] == "crash" else
                                      FAULTS if value["mode"] == "late" else (None,))):
        raise ValueError("invalid recovery fixture control")
    return value


def mark(root, stage, **details):
    owned.save(root / ("probe-" + stage + ".json"), {"stage": stage, "root": str(root),
               "pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgrp(), "sid": os.getsid(0),
               "at": time.time(), **details})


def pause(root, selected, stage, **details):
    if selected["mode"] == "crash" and selected["stage"] == stage:
        mark(root, stage, **details)
        while not (root / "release").exists():
            time.sleep(0.01)


@contextlib.contextmanager
def parent_hooks(root, selected):
    run, append, trigger, publish, invoke = owned.run, owned.Campaign.append, owned.append_trigger, owned.publish_config, owned.invoke

    async def staged(*args, **kwargs):
        if kwargs.pop("stage", None) is not None:
            raise ValueError("recovery wrapper cannot replace another stage hook")

        async def stage(name, state):
            pause(root, selected, name, state=state.summary())

        return await run(*args, **kwargs, stage=stage)

    def intake(journal, kind, **kwargs):
        is_intake = kind == "acknowledgement" and kwargs.get("data", {}).get("phase") == "intake"
        if is_intake:
            pause(root, selected, "before_intake")
        result = append(journal, kind, **kwargs)
        if is_intake:
            pause(root, selected, "after_intake")
        return result

    def after_trigger(*args):
        result = trigger(*args)
        pause(root, selected, "trigger", trigger=result)
        return result

    def after_config(*args):
        result = publish(*args)
        pause(root, selected, "config", nonce=result["HEADLONG_REFERENCE_NONCE"])
        return result

    async def wrapped_child(argv, *args):
        if argv[:2] != [sys.executable, "/fixture/headlong_native_bridge.py"]:
            raise ValueError("unexpected recovery child executable")
        return await invoke([argv[0], "/fixture/ego_owned_probe.py", "--native", *argv[2:]], *args)

    with contextlib.ExitStack() as stack:
        for name, replacement in (("run", staged), ("append_trigger", after_trigger),
                                  ("publish_config", after_config), ("invoke", wrapped_child)):
            stack.enter_context(patch.object(owned, name, replacement))
        stack.enter_context(patch.object(owned.Campaign, "append", intake))
        yield


@contextlib.contextmanager
def native_hooks(root, selected, config):
    run = Path(config["run_root"])
    turn, write = bridge.native.native_turn, bridge.native.write_effect
    connect, original_open, replace, output = bridge.aiosqlite.connect, Path.open, Path.replace, sys.stdout
    phase = selected["stage"] if selected["mode"] == "late" else None

    async def recorded_turn(*args):
        result, messages, stderr = await turn(*args)
        owned.save(run / "probe-native.json", {"native": result, "messages": messages, "stderr": stderr,
                   "nonce": config["invocation_id"]})
        return result, messages, stderr

    def observed_effect(directory):
        pause(root, selected, "claimed", nonce=config["invocation_id"])
        write(directory)
        pause(root, selected, "effect", nonce=config["invocation_id"])

    def trip():
        mark(root, "late", fault=phase, nonce=config["invocation_id"])
        raise OSError("synthetic late fault " + phase)

    class CloseFault:
        def __init__(self, *args, **kwargs):
            self.connection = connect(*args, **kwargs)

        async def __aenter__(self):
            return await self.connection.__aenter__()

        async def __aexit__(self, *args):
            await self.connection.__aexit__(*args)
            trip()

    class StreamFault:
        def __init__(self, path, *args, **kwargs):
            self.stream = original_open(path, *args, **kwargs)

        def __enter__(self):
            return self

        def write(self, text):
            if phase == "driver_short":
                mark(root, "late", fault=phase, nonce=config["invocation_id"])
                return self.stream.write(text[:3])
            return self.stream.write(text)

        def __exit__(self, *args):
            self.stream.close()
            if phase == "driver_close":
                trip()

    def pending_open(path, *args, **kwargs):
        if path == run / "driver.pending":
            if phase == "driver_open":
                trip()
            return StreamFault(path, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    def pending_replace(path, destination):
        if path == run / "driver.pending":
            trip()
        return replace(path, destination)

    class OutputFault:
        seen_final = False

        def __getattr__(self, name):
            return getattr(output, name)

        def write(self, text):
            if text == "native-final-approved":
                self.seen_final = True
                if phase == "stdout":
                    trip()
            return output.write(text)

        def flush(self):
            if self.seen_final and phase == "stdout_flush":
                trip()
            return output.flush()

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(bridge.native, "native_turn", recorded_turn))
        stack.enter_context(patch.object(bridge.native, "write_effect", observed_effect))
        if phase == "db_close":
            stack.enter_context(patch.object(bridge.aiosqlite, "connect", CloseFault))
        elif phase and phase.startswith("driver_"):
            stack.enter_context(patch.object(Path, "open", pending_open))
        elif phase == "rename":
            stack.enter_context(patch.object(Path, "replace", pending_replace))
        elif phase in {"stdout", "stdout_flush"}:
            stack.enter_context(patch.object(sys, "stdout", OutputFault()))
        yield


async def main(work_root=Path("/work")):
    preflight()
    if sys.argv[1:2] == ["--native"]:
        config = load_config(os.environ, work_root)
        root = Path(config["root"])
        selected = control(root)
        sys.argv = ["headlong_native_bridge.py", *sys.argv[2:]]
        with native_hooks(root, selected, config):
            code = await bridge.main(work_root)
            pause(root, selected, "receipt", nonce=config["invocation_id"], returncode=code)
            return code
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, required=True)
    args, _ = parser.parse_known_args()
    root = args.root.resolve()
    if root == work_root.resolve() or not root.is_relative_to(work_root.resolve()):
        raise ValueError("recovery fixture root outside owned work")
    selected = control(root)
    with parent_hooks(root, selected):
        return await owned.main(work_root)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(owned.json.dumps({"schema": "ego-owned-probe/v1", "status": "incomplete",
                               "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(2) from exc
