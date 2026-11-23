#!/usr/bin/env bash
# Run only in CI's credential-free, network-none qualification container.
set -euo pipefail
mkdir -p /work/home
status=0
python -m pytest -q -p tests.test_hooks.codex_elicitation_capture \
  tests/test_hooks/test_codex_elicitation_native.py \
  -o tmp_path_retention_policy=all --basetemp=/evidence/native \
  --junitxml=/evidence/junit.xml || status=$?
# Preserve a refusal report even when native behavior falsifies the hypothesis.
python -m scripts.ci.codex_elicitation_readback \
  /fixture/elicitation-expected.json /evidence/elicitation-report.json /evidence/native \
  --head "$QUALIFICATION_HEAD" --base "$QUALIFICATION_BASE" \
  --run-id "$ELICITATION_RUN_ID" --attempt "$ELICITATION_ATTEMPT" \
  > /evidence/elicitation-readback.json
exit "$status"
