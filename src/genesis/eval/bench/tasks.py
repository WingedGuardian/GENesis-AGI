"""Private-JSONL task loader for the A/B bench.

The task set derives from real user history (session topics, Telegram
requests) — verbatim-private — so it lives OUTSIDE the repo and must never
enter it. Two privacy gates:

  1. In code: ``load_tasks`` raises if the task file resolves inside the
     repo tree (the public repo ships only synthetic exemplars under
     ``tests/test_eval/bench_fixtures/``, which the tests load via an
     explicit ``allow_repo_path`` escape hatch).
  2. In process: the pre-push privacy grep of the whole diff.

File format: JSON Lines. An optional first line ``{"_meta": {...}}`` carries
``task_set_version``; every other line is one task::

    {"id": "recall_003", "category": "recall",
     "prompt": "...", "expected": "<ex-ante success criteria>",
     "context": "...", "timeout_s": 1200}

``expected`` is EX-ANTE and frozen: the loader records the file's sha256 so a
post-hoc edit changes the recorded hash and breaks run-to-run comparability
visibly rather than silently.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from genesis.env import genesis_home, repo_root
from genesis.eval.bench.types import (
    DEFAULT_TASK_TIMEOUT_S,
    VALID_CATEGORIES,
    BenchTask,
)

#: Default private task-set location (reflection-golden-set precedent:
#: private eval data lives under ~/.genesis/, never in the repo).
DEFAULT_TASKS_PATH = genesis_home() / "eval" / "bench_tasks_v1.jsonl"

_REQUIRED_FIELDS = ("id", "category", "prompt", "expected")


class TaskFileError(ValueError):
    """Raised for any task-file problem — the CLI maps this to exit code 2."""


def load_tasks(
    path: Path | str = DEFAULT_TASKS_PATH,
    *,
    allow_repo_path: bool = False,
) -> tuple[list[BenchTask], str, str]:
    """Load and validate the bench task set.

    Returns ``(tasks, task_set_version, file_sha256)``.

    Args:
        path: JSONL task file.
        allow_repo_path: ONLY for tests loading the synthetic fixtures that
            ship in-repo. Production callers must never set this — real task
            files inside the repo tree are a privacy leak by construction.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise TaskFileError(f"task file not found: {path}")

    resolved = path.resolve()
    if not allow_repo_path:
        try:
            resolved.relative_to(repo_root().resolve())
        except ValueError:
            pass  # outside the repo — good
        else:
            raise TaskFileError(
                f"task file {resolved} is INSIDE the repo tree. Bench tasks "
                "derive from private history and must live outside the repo "
                f"(default: {DEFAULT_TASKS_PATH}). Refusing to load."
            )

    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()

    tasks: list[BenchTask] = []
    seen_ids: set[str] = set()
    version = "unversioned"

    for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TaskFileError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise TaskFileError(f"{path}:{lineno}: expected a JSON object")

        if "_meta" in record:
            if lineno != 1:
                raise TaskFileError(
                    f"{path}:{lineno}: _meta is only valid on the first line"
                )
            meta = record["_meta"] or {}
            version = str(meta.get("task_set_version", version))
            continue

        missing = [f for f in _REQUIRED_FIELDS if not record.get(f)]
        if missing:
            raise TaskFileError(
                f"{path}:{lineno}: missing required field(s): {', '.join(missing)}"
            )
        category = str(record["category"])
        if category not in VALID_CATEGORIES:
            raise TaskFileError(
                f"{path}:{lineno}: unknown category {category!r} "
                f"(valid: {', '.join(sorted(VALID_CATEGORIES))})"
            )
        task_id = str(record["id"])
        if task_id in seen_ids:
            raise TaskFileError(f"{path}:{lineno}: duplicate task id {task_id!r}")
        seen_ids.add(task_id)

        timeout_raw = record.get("timeout_s", DEFAULT_TASK_TIMEOUT_S)
        try:
            timeout_s = int(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise TaskFileError(
                f"{path}:{lineno}: timeout_s must be an integer, got {timeout_raw!r}"
            ) from exc
        if timeout_s <= 0:
            raise TaskFileError(f"{path}:{lineno}: timeout_s must be positive")

        tasks.append(
            BenchTask(
                id=task_id,
                category=category,
                prompt=str(record["prompt"]),
                expected=str(record["expected"]),
                context=str(record.get("context", "") or ""),
                timeout_s=timeout_s,
            )
        )

    if not tasks:
        raise TaskFileError(f"{path}: no tasks found")

    return tasks, version, sha256
