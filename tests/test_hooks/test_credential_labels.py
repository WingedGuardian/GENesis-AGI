"""Structured-label API stays coupled to the historical prose policy."""

import json
from pathlib import Path


def test_label_api_and_plaintext_differential():
    path = Path(__file__).resolve().parents[2] / "scripts/hooks/secret_scrub.py"
    namespace = {}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)  # noqa: S102 - trusted source
    fixtures = path.parents[2] / "tests/fixtures/credential-label-plaintext.json"
    for text, expected in json.loads(fixtures.read_text()):
        assert namespace["scrub"](text) == expected
    helper = namespace["credential_label_kind"]
    assert helper("GH_TOKEN") == "env"
    assert helper("GH_TOKEN  ") == "env"
    assert helper("api_key ") == "token"
    assert helper("SERVICE_AUTH") == "env"
    assert helper("api_key") == "token"
    assert helper("DB_PASSWORD") == "password"
    assert helper("monkey") is None
    assert helper("api_version") is None
    assert helper("somePassword") is None


def test_classifier_boundary_classes():
    """Pin inherited grammar and precedence, independently of value scrubbing."""
    path = Path(__file__).resolve().parents[2] / "scripts/hooks/secret_scrub.py"
    namespace = {}
    exec(compile(path.read_bytes(), str(path), "exec"), namespace)  # noqa: S102 - trusted source
    helper = namespace["credential_label_kind"]
    cases = [
        ("API_KEY", "token"), ("DB_PASSWORD", "password"),
        ("api-key", "token"), ("api key", "token"),
        ("api\tkey", "token"), ("api\u00a0key", "token"),
        ("api__key", None), ("apİ_key", "token"),
        ("key_api_key", "token"), ("monkeyapi_key", None),
        ("gh_TOKEN", "env"), ("_____API", "env"),
        ("API", "env"), ("AP", None),
        ("A" * 510 + "API", "env"), ("A" * 511 + "API", None),
        ("A" * 510 + "_API", "env"), ("secret\nkey", "token"),
        ("secret key\u200b", None), ("api_key\0", None),
        ("password\u00a0", "password"), ("somePassword", None),
        ("123password", "password"), ("", None),
    ]
    for label, expected in cases:
        assert helper(label) == expected, repr(label)
