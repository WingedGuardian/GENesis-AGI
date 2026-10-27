"""Early actual C1 smoke; execute only in the disposable offline CI image."""

import asyncio
import base64
import contextlib
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import aiosqlite
import ego_owned_loop as owned
import headlong_native_controls as native
from ego_runtime_report import valid_c_counters
from headlong_controls import dependency_inventory, preflight, read_jsonl, tree_hash
from headlong_monolith_controls import (
    CROSSING,
    MARKERS,
    MENTION_CONTENT,
    append,
    captured_text,
    command,
)
from headlong_native_bridge import ReceiptView
from headlong_native_provider import Backend
from headlong_prompt_references import canonical_id


async def seed(root, name, *, mechanics=None):
    if mechanics is not None and mechanics.name != name:
        raise ValueError("misbound shared mechanics fixture")
    if mechanics is None:
        approved, reactive, conflict, large = name == "restart_effect", name == "reactive", name == "blocked", False
    else:
        approved, reactive, conflict, large = mechanics.kind == "approved", mechanics.reactive, mechanics.blocked, name == "large_context"
    root.mkdir()
    identity = root / "identity"
    for sub in ("run", "workdir", "memories", "skills/fixture", "kernel", "trajectories"):
        (identity / sub).mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir()
    spec = {"root": str(root), "executor": "claude-code", "model": "sonnet", "ego_source": "genesis_ego_cycle",
            "source_pin": owned.source_pin(), "sources": owned.source_hashes(), "trajectory_id": "",
            "reference_ids": [], "start": time.time(), "feed": [], "responses": [],
            "pacing": {"base": 5, "factor": 2, "cap": 40, "hold": 1}}
    env = owned.environment(root, spec)
    created = command(["/headlong-b/bin/traj", "new", "--traj_dir", env["TRAJ_DIR"], "--slug", "fixture"], env, root)
    if created["returncode"]:
        raise ValueError("C1 seed trajectory creation failed")
    spec["trajectory_id"] = canonical_id(created["stdout"].splitlines()[0])
    env = owned.environment(root, spec)
    spec["reference_ids"] = [spec["trajectory_id"], canonical_id(append({"type": "observation", "source": "fixture",
        "content": "continuity orbit fixture-stream-marker"}, env, root))]
    (identity / "info.txt").write_text(f"name=fixture\nroot_trajectory={spec['trajectory_id']}\n")
    (identity / "core_identity_prompt.md").write_text("fixture-identity-marker λ @mention-target.txt\n" +
        ("continuity context " * 9000 if large else ""))
    (identity / "workdir/mention-target.txt").write_text(MENTION_CONTENT)
    (identity / "memories/goal.md").write_text("---\ntype: goal\ncreated: 2000-01-01\n---\nfixture-goal-marker\n")
    (identity / "memories/continuity-note.md").write_text("---\ntype: memory\ncreated: 2000-01-01\n---\ncontinuity orbit fixture-memory-marker\n")
    (identity / "skills/fixture/SKILL.md").write_text("---\nname: fixture\ndescription: fixture-skill-marker\n---\nSynthetic context only.\n")
    if conflict:
        spec["reference_ids"].append(canonical_id(append({"type": "observation", "source": "fixture",
            "step_id": CROSSING, "content": "owned crossing control"}, env, root)))
        with (identity / "core_identity_prompt.md").open("a") as stream:
            stream.write(CROSSING + " 1111 1111\n")
    if reactive:
        spec["feed"] = [{"id": "external-1", "seq": 1, "due": spec["start"], "source": "fixture",
                         "target": "genesis_ego_cycle", "type": "observation", "content": "continuity orbit wake"}]
    async with aiosqlite.connect(root / "fixture.sqlite3") as db:
        db.row_factory = aiosqlite.Row
        for table in ("ego_proposals", "ego_proposal_revisions"):
            await db.execute(native.TABLES[table])
        await native.prepare_case(db, "approved" if approved else "pending")
    return spec


def retained(root):
    # Retain only synthetic evidence/context; SDK HOME caches are not evidence.
    paths = [root / "manifest.json", root / "journal/answers.jsonl", root / "fixture.sqlite3"]
    paths += [p for p in (root / "identity").rglob("*") if p.is_file()]
    paths += [p for run in root.glob("attempt-*") for p in run.iterdir() if p.is_file()]
    if (root / "effect.bin").exists():
        paths.append(root / "effect.bin")
    return [{"path": str(p.relative_to(root)), "size": p.stat().st_size, "sha256": owned.sha(p.read_bytes()),
             "content_b64": base64.b64encode(p.read_bytes()).decode()} for p in sorted(paths) if p.is_file()]


def qualify(name, case):
    reports, backends = case["reports"], case["backends"]
    if not reports or any(b["unexpected"] for b in backends):
        return False
    report = reports[-1]
    observations = report["observations"]
    expected = {"timers": ["noop", "noop"], "restart_effect": ["completed", "refused"],
                "reactive": ["noop"], "blocked": ["blocked"]}[name]
    if [row["outcome"] for row in observations] != expected:
        return False
    if (not valid_c_counters(report["state"])
            or case["effect"] != (native.EFFECT.hex() if name == "restart_effect" else None)
            or case["state"]["status"] != ("executed" if name == "restart_effect" else "pending")
            or case["groups_closed"] is not True
            or report["state"]["cursor"] != int(name == "reactive")
            or report["state"]["timer_done"] != (0 if name in {"reactive", "blocked"} else 2)
            or report["status"] != ("blocked" if name == "blocked" else "budget")
            or any(r["returncode"] != (2 if name == "blocked" else 0) or r["timed_out"] for r in case["commands"])):
        return False
    if name == "blocked":
        driver = observations[0]["evidence"]["driver"]
        return (all(b["main_requests"] == 0 for b in backends) and driver["native"] is None
                and driver["complete"] is False and driver["prompt_status"] == "65"
                and driver["failure_kind"] == "reference_conflict" and case["prompts"] == [""]
                and driver["calls"] == [] and driver["observation_id"] is driver["final_id"] is None)
    sessions = []
    for index, observation in enumerate(observations):
        evidence, backend = observation["evidence"], backends[index]
        driver = evidence["driver"]
        sessions.append(driver["native"]["session_id"])
        prompt = case["prompts"][index]
        captured = captured_text(backend)
        kind = "approved" if name == "restart_effect" and index == 0 else "pending" if name == "restart_effect" else "echo"
        if (driver["complete"] is not True or driver["outer_source"] != "ego-owned-loop" or backend["main_requests"] < 1
                or driver["native"]["is_error"] is not False or driver["native"]["subtype"] != "success"
                or driver["native"]["result"] != "native-final-" + kind
                or any(marker not in prompt for marker in MARKERS if index == 0 or marker != "fixture-memory-marker")
                or prompt not in captured or "@mention-target.txt" not in captured or MENTION_CONTENT in captured
                or evidence["errors"] or driver["prompt_sha256"] != owned.sha(prompt.encode())):
            return False
    if len(set(sessions)) != len(sessions):
        return False
    if name == "timers":
        times = [o["observed_at"] for o in observations]
        if times[1] - times[0] < 4.9 or report["state"]["wake_at"] != times[1] + 10:
            return False
    if name == "restart_effect":
        previous = observations[0]["evidence"]["references"]
        # Recent stream abbreviates IDs but retains the full details lookup pointer.
        if previous[0] not in case["prompts"][1] or "fixture receipt proposal=proposal" not in case["prompts"][1]:
            return False
        calls = [o["evidence"]["calls"] for o in observations]
        if calls != [[{"canonical_called": True, "claim": True, "outcome": "executed"}],
                     [{"canonical_called": True, "claim": False, "outcome": "refused"}]]:
            return False
    return True


async def scenario(root, name):
    spec = await seed(root, name)
    kinds = ["approved", "pending"] if name == "restart_effect" else ["echo", "echo"] if name == "timers" else ["echo"]
    reports, commands = [], []
    with contextlib.ExitStack() as stack:
        backends = [stack.enter_context(Backend(kind, ReceiptView(root / f"attempt-{i:05d}/calls.jsonl")))
                    for i, kind in enumerate(kinds, 1)]
        spec["responses"] = [{"kind": kind, "backend_port": backend.server.server_port, "scenario": name,
                              "fault": None, "fixture_header_id": None} for kind, backend in zip(kinds, backends, strict=True)]
        owned.save(root / "manifest.json", spec)
        # Separate processes establish actual journal reopening; no fixture cursor edits.
        for steps in ([1, 1] if name == "restart_effect" else [len(kinds)]):
            result = command([sys.executable, "/fixture/ego_owned_loop.py", "--root", str(root), "--steps", str(steps)],
                             owned.environment(root, spec), root)
            commands.append(result)
            reports.append(json.loads(result["stdout"]))
        evidence = [backend.evidence for backend in backends]
    after = await owned.snapshot(root)
    groups_closed = all((run / "child-closed.json").is_file()
                        and not owned.group_alive(json.loads((run / "child.json").read_text())["pgid"])
                        for run in root.glob("attempt-*"))
    case = {"scenario": name, "reports": reports, "commands": commands, "backends": evidence,
            "state": after["state"], "effect": after["effect"], "trajectory": read_jsonl(owned.trajectory(root)),
            "prompts": [(run / "raw-prompt.txt").read_text() for run in sorted(root.glob("attempt-*"))],
            "artifacts": retained(root), "groups_closed": groups_closed}
    case["control_passed"] = qualify(name, case)
    return case


async def main():
    preflight()
    import claude_agent_sdk

    cli = Path(claude_agent_sdk.__file__).parent / "_bundled/claude"
    sources = {name: tree_hash(Path(path)) for name, path in
               {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}.items()}
    with TemporaryDirectory(dir="/work") as directory:
        cases = []
        for name in ("timers", "restart_effect", "reactive", "blocked"):
            root = Path(directory) / name
            try:
                cases.append(await scenario(root, name))
            except Exception as exc:
                # Keep controlled failure evidence rather than losing it at cleanup.
                cases.append({"scenario": name, "control_passed": False, "groups_closed": False,
                              "error": f"{type(exc).__name__}: {exc}",
                              "artifacts": retained(root) if (root / "manifest.json").exists() else []})
    unchanged = sources == {name: tree_hash(Path(path)) for name, path in
                           {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}.items()}
    passed = unchanged and all(case["control_passed"] for case in cases)
    print(json.dumps({"schema": "ego-owned-smoke/v1", "status": "pass" if passed else "incomplete", "cases": cases,
                      "source_pin": owned.source_pin(), "sources": sources, "sources_unchanged": unchanged,
                      "cleanup_confirmed": all(case["groups_closed"] for case in cases),
                      "sdk": importlib.metadata.version("claude-agent-sdk"),
                      "native_binary": {"sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
                                        "version": subprocess.check_output([str(cli), "--version"], text=True, timeout=10).strip()},
                      "dependencies": dependency_inventory(), "profile": "early C1 mechanics smoke; scripted model; no full comparison or runtime parity"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
