"""Private disposable-profile configuration, never a production approval authority."""

import json
import os
import re
import sys
import uuid
from pathlib import Path


def canonical_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("noncanonical issued reference")
    return value


def load_config(env, work_root=Path("/work")):
    root = Path(env["HEADLONG_REFERENCE_ROOT"]).resolve()
    run = Path(env["HEADLONG_REFERENCE_RUN_ROOT"]).resolve()
    path = Path(env["CONTROL_CONFIG"]).resolve()
    if not root.is_relative_to(work_root.resolve()) or run.parent != root or path != run / "config.json":
        raise ValueError("misbound reference configuration path")
    if (run / "config.pending").exists():
        raise ValueError("pending reference configuration")
    config = json.loads(path.read_text())
    expected = {"root": str(root), "run_root": str(run), "identity_dir": str(root / "identity"),
                "traj_id": env["TRAJ_ID"], "trigger_id": env["HEADLONG_REFERENCE_TRIGGER"],
                "invocation_id": env["HEADLONG_REFERENCE_NONCE"]}
    if (any(config.get(key) != value for key, value in expected.items())
            or env["IDENTITY_DIR"] != expected["identity_dir"] or not expected["traj_id"]
            or not re.fullmatch(r"[0-9a-f]{32}", expected["invocation_id"])):
        raise ValueError("misbound reference configuration")
    canonical_id(expected["trigger_id"])
    refs = config["reference_ids"]
    if not isinstance(refs, list) or any(canonical_id(ref) != ref for ref in refs) or len(set(refs)) != len(refs):
        raise ValueError("invalid issued references")
    return config


def publish_config(config, env):
    """Called only by the closed fixture controller with retained writer returns."""
    config = {**config, "invocation_id": uuid.uuid4().hex}
    root, run = Path(config["root"]), Path(config["run_root"])
    pending, path = run / "config.pending", run / "config.json"
    if path.exists():
        raise ValueError("reused reference invocation")
    with pending.open("x") as stream:
        stream.write(json.dumps(config))
    pending.replace(path)
    return {**env, "CONTROL_CONFIG": str(path), "HEADLONG_REFERENCE_ROOT": str(root),
            "HEADLONG_REFERENCE_RUN_ROOT": str(run), "HEADLONG_REFERENCE_NONCE": config["invocation_id"],
            "HEADLONG_REFERENCE_TRIGGER": config["trigger_id"]}


if __name__ == "__main__":
    try:
        print(json.dumps(load_config(os.environ)["reference_ids"]))
    except (KeyError, ValueError, TypeError, AttributeError, OSError):
        print("invalid reference configuration", file=sys.stderr)
        raise SystemExit(2) from None
