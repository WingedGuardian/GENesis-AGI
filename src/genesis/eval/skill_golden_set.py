"""Author a per-skill golden task suite for the held-out replay gate (WS1).

Writes a starter suite to ``~/.genesis/eval/skill_golden/<skill>.jsonl`` — OUTSIDE
the repo, because per-skill suites derive from how YOU use the skill and stay
private (like the bench task set). The ``expected`` field is EX-ANTE frozen
success criteria you edit by hand: the replay judge grades an arm's output
against exactly these, and the loader records the file's sha256 so a post-hoc
edit is visible rather than silent.

Author suites for the behavior the SKILL.md BODY controls (process / structure /
what the output must or must not do) — NOT criteria that need files or exemplars
the isolated replay can't reach.

Usage::

    python -m genesis.eval.skill_golden_set --skill voice-master        # scaffold
    python -m genesis.eval.skill_golden_set --validate <path/to/suite.jsonl>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from genesis.env import genesis_home


def default_suite_path(skill_name: str) -> Path:
    return genesis_home() / "eval" / "skill_golden" / f"{skill_name}.jsonl"


# A minimal starter the author replaces with real tasks. Valid JSON (loads
# cleanly) but every field is a placeholder flagged by --validate until edited.
_SCAFFOLD_TASKS = [
    {
        "id": "example_1",
        "category": "drafting",
        "prompt": "<REPLACE: a real task you would give this skill>",
        "expected": (
            "<REPLACE: frozen, checkable criteria for the SKILL.md BODY behavior — "
            "what the output must and must not do; avoid anything needing external files>"
        ),
    },
]


def write_scaffold(skill_name: str, path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"{path} already exists — use --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_meta": {"task_set_version": f"{skill_name}_v1"}})]
    lines += [json.dumps(t) for t in _SCAFFOLD_TASKS]
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote scaffold: {path}")
    print("Edit each task's prompt + expected (8-15 tasks recommended), then --validate.")


def validate(path: Path) -> int:
    from genesis.eval.bench.tasks import TaskFileError, load_tasks

    try:
        tasks, version, sha = load_tasks(path)
    except TaskFileError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {len(tasks)} task(s), version={version}, sha256={sha[:12]}…")
    placeholders = [t.id for t in tasks if "<REPLACE" in t.expected or "<REPLACE" in t.prompt]
    if placeholders:
        print(f"  WARNING: unedited placeholder task(s): {', '.join(placeholders)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Author a per-skill golden suite (WS1).")
    ap.add_argument("--skill", help="skill name to scaffold a suite for")
    ap.add_argument("--output", type=Path, help="override the output path")
    ap.add_argument("--force", action="store_true", help="overwrite an existing suite")
    ap.add_argument("--validate", type=Path, help="validate an existing suite file")
    args = ap.parse_args(argv)

    if args.validate:
        return validate(args.validate)
    if not args.skill:
        ap.error("--skill (to scaffold) or --validate is required")
    write_scaffold(args.skill, args.output or default_suite_path(args.skill), force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
