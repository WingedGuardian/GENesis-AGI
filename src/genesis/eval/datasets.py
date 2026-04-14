"""Golden dataset loader — reads YAML eval datasets from config/eval_datasets/."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from genesis.eval.types import EvalCase, ScorerType, TaskCategory

logger = logging.getLogger(__name__)

_DATASETS_DIR = Path(__file__).resolve().parents[3] / "config" / "eval_datasets"


def load_dataset(name: str, *, datasets_dir: Path | None = None) -> list[EvalCase]:
    """Load a named dataset (without .yaml extension).

    Raises FileNotFoundError if the dataset doesn't exist.
    Raises ValueError on malformed entries.
    """
    base = datasets_dir or _DATASETS_DIR
    path = (base / f"{name}.yaml").resolve()
    if not str(path).startswith(str(base.resolve())):
        raise ValueError(f"dataset name contains path traversal: {name!r}")
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {path}")
    return _parse_dataset_file(path)


def list_datasets(*, datasets_dir: Path | None = None) -> list[str]:
    """List available dataset names."""
    base = datasets_dir or _DATASETS_DIR
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.yaml"))


def _parse_dataset_file(path: Path) -> list[EvalCase]:
    """Parse a YAML dataset file into EvalCase objects."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"dataset {path.name}: expected a YAML mapping at top level")

    metadata = raw.get("metadata", {})
    default_scorer = metadata.get("default_scorer", "exact_match")
    default_category = metadata.get("category", "classification")

    cases_raw = raw.get("cases", [])
    if not cases_raw:
        logger.warning("Dataset %s has no cases", path.name)
        return []

    cases: list[EvalCase] = []
    for i, entry in enumerate(cases_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"dataset {path.name} case #{i}: expected a mapping")
        case_id = entry.get("id", f"{path.stem}_{i}")
        input_text = entry.get("input")
        expected = entry.get("expected")
        if input_text is None or expected is None:
            raise ValueError(
                f"dataset {path.name} case {case_id}: 'input' and 'expected' required"
            )
        scorer = ScorerType(entry.get("scorer", default_scorer))
        category = TaskCategory(entry.get("category", default_category))
        scorer_config = entry.get("scorer_config", {})
        description = entry.get("description", "")

        cases.append(EvalCase(
            id=case_id,
            input_text=str(input_text),
            expected_output=str(expected),
            scorer_type=scorer,
            scorer_config=scorer_config,
            category=category,
            description=description,
        ))

    return cases
