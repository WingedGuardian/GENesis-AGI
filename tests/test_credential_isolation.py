"""Test-time isolation of Genesis credentials."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from genesis.env import CREDENTIAL_NAME_RE, credential_env_names, secrets_path


def test_credential_names_are_pinned_empty():
    assert all(os.environ[name] == "" for name in credential_env_names())


def test_secrets_path_points_to_missing_file():
    assert secrets_path() == Path(os.environ["SECRETS_PATH"])
    assert not secrets_path().exists()


def test_child_inheriting_environment_sees_empty_credentials():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os; print(json.dumps({"
            "'API_KEY_GROQ': os.environ.get('API_KEY_GROQ'), "
            "'OPENAI_API_KEY': os.environ.get('OPENAI_API_KEY')}))",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.stdout.strip() == '{"API_KEY_GROQ": "", "OPENAI_API_KEY": ""}'


def test_groq_key_resolver_returns_none():
    from genesis.routing.litellm_delegate import _resolve_api_key

    assert _resolve_api_key("groq") is None


def test_fixed_path_dotenv_cannot_replace_empty_pin(tmp_path):
    planted = tmp_path / "secrets.env"
    planted.write_text("API_KEY_GROQ=planted\n", encoding="utf-8")

    load_dotenv(planted, override=False)

    assert os.environ["API_KEY_GROQ"] == ""


def test_credential_regex_covers_example_provider_keys():
    example = Path(__file__).resolve().parents[1] / "secrets.env.example"
    candidates = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", line)
        if match:
            name = match.group(1)
            if name.startswith("API_KEY_") or name.endswith(("_API_KEY", "_TOKEN")):
                candidates.add(name)

    assert candidates
    assert all(CREDENTIAL_NAME_RE.search(name) for name in candidates)
