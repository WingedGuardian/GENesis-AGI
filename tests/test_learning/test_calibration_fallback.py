"""Triage calibration falls back to the .example seed (Codex audit idx 50).

install.sh-only setups may lack the runtime TRIAGE_CALIBRATION.md (it's
gitignored / runtime-generated) while the committed .example seed is present.
_load_calibration now falls back to the .example so triage runs with real
calibration instead of an empty prompt, rather than returning "".
"""

from __future__ import annotations

from unittest.mock import MagicMock

from genesis.learning.triage.classifier import TriageClassifier


def _clf(path):
    return TriageClassifier(router=MagicMock(), calibration_path=path)


def test_falls_back_to_example_when_primary_missing(tmp_path):
    (tmp_path / "TRIAGE_CALIBRATION.md.example").write_text("SEED-CALIBRATION")
    clf = _clf(tmp_path / "TRIAGE_CALIBRATION.md")  # primary absent
    assert clf._load_calibration() == "SEED-CALIBRATION"


def test_primary_wins_over_example(tmp_path):
    (tmp_path / "TRIAGE_CALIBRATION.md").write_text("LIVE-CALIBRATION")
    (tmp_path / "TRIAGE_CALIBRATION.md.example").write_text("SEED-CALIBRATION")
    clf = _clf(tmp_path / "TRIAGE_CALIBRATION.md")
    assert clf._load_calibration() == "LIVE-CALIBRATION"


def test_empty_when_neither_exists(tmp_path):
    clf = _clf(tmp_path / "TRIAGE_CALIBRATION.md")  # no .md, no .example
    assert clf._load_calibration() == ""
