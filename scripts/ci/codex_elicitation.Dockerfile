# Extend the parent fixture's pinned packages, native binary and containment.
FROM codex-qualification
USER root
ARG PROBE_RUN_ID
ARG PROBE_ATTEMPT
ENV ELICITATION_RUN_ID=${PROBE_RUN_ID} ELICITATION_ATTEMPT=${PROBE_ATTEMPT} \
    ELICITATION_MANIFEST=/fixture/elicitation-expected.json \
    ELICITATION_REPORT=/evidence/elicitation-report.json
# Collection only: this freezes a separate17-case census without running Codex.
RUN python -m pytest --collect-only -q -p tests.test_hooks.codex_elicitation_capture \
    tests/test_hooks/test_codex_elicitation_native.py
USER 65532:65532
CMD ["bash", "/fixture/scripts/ci/codex_elicitation.sh"]
