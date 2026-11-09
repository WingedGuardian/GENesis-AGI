FROM python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
RUN apt-get update && apt-get install -y --no-install-recommends bash git jq procps && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin
COPY .github/test-tools/requirements.txt /opt/test-tools.txt
RUN pip install --no-cache-dir -c /opt/test-tools.txt pytest pytest-asyncio pytest-timeout fastmcp==4.1.5 aiosqlite==0.22.1 pyyaml==6.0.3 python-dotenv==1.2.2 regex==2026.3.32 aiohttp==3.14.5 litellm==1.100.4 apscheduler==3.11.2
# Public reference package. Verify before extraction; preserve native companion layout.
RUN python - <<'PY'
import base64, hashlib, io, tarfile, urllib.request
data = urllib.request.urlopen('https://registry.npmjs.org/@openai/codex/-/codex-0.166.0-linux-x64.tgz', timeout=120).read()
assert base64.b64encode(hashlib.sha512(data).digest()).decode() == 'CxDm3+D1cpbGJeHrdzINoG+3wn9BVncP4qpZaaNYkxe8iPtRJOYyCJnnPoVjWRIBvI9vPKbRoifsMSA5xDaUpw=='  # pragma: allowlist secret (public npm package SHA-512 integrity)
with tarfile.open(fileobj=io.BytesIO(data)) as archive:
    archive.extractall('/opt/codex', filter='data')
PY
COPY . /fixture/
WORKDIR /fixture
ARG SOURCE_HEAD
ARG SOURCE_BASE
ENV HOME=/work/home PYTHONPATH=/fixture/src PYTHONDONTWRITEBYTECODE=1 \
    GENESIS_CODEX_NATIVE_TESTS=1 \
    GENESIS_CODEX_CLI_BIN=/opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex \
    GENESIS_CODEX_APP_SERVER_BIN=/opt/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex \
    QUALIFICATION_HEAD=${SOURCE_HEAD} QUALIFICATION_BASE=${SOURCE_BASE} \
    QUALIFICATION_MANIFEST=/fixture/expected.json QUALIFICATION_REPORT=/evidence/report.json
# Exercise shared autouse setup without starting any native executable.
RUN mkdir -p /work/home && python -m pytest -q tests/test_hooks/test_codex_fixture_driver.py
# Freeze native collection and source/binary hashes before any native test runs.
RUN python -m pytest --collect-only -q -p tests.test_hooks.codex_qualification \
    tests/test_hooks/test_codex_native.py tests/test_hooks/test_codex_validator_adapter_native.py \
    tests/test_hooks/test_codex_compatibility_native.py
RUN pip freeze > /fixture/python-packages.txt && dpkg-query -W > /fixture/os-packages.txt
RUN mkdir /evidence && chown 65532:65532 /evidence
USER 65532:65532
CMD ["bash", "/fixture/scripts/ci/codex_qualification.sh"]
