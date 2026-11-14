#!/usr/bin/env bash
# Deterministic fixtures only; run in the CI containment, never a live profile.
set -euo pipefail
mkdir -p /work/home
python -m pytest -q -p tests.test_hooks.codex_qualification \
  tests/test_hooks/test_codex_native.py tests/test_hooks/test_codex_validator_adapter_native.py \
  tests/test_hooks/test_codex_compatibility_native.py \
  -o tmp_path_retention_policy=all --basetemp=/evidence/native --junitxml=/evidence/junit.xml
python scripts/ci/codex_qualification_readback.py /fixture/expected.json /evidence/report.json /evidence/native \
  --head "$QUALIFICATION_HEAD" --base "$QUALIFICATION_BASE" > /evidence/readback.json
