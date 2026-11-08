"""Approved publication shapes; synthetic credentials are assembled at runtime."""
from types import SimpleNamespace

import pytest

from genesis.contribution import sanitize


def _key(prefix="AKIA", length=16):
    return prefix + "Q" * length


def _url(password="synthetic", user="fixture", scheme="https"):  # noqa: S107 - synthetic fixture
    return f"{scheme}://{user}:{password}@example.invalid/path"


@pytest.mark.parametrize("text,category", [
    (_key(), "aws-key-id"), (_key("ASIA"), "aws-key-id"),
    (_key(length=12), "aws-key-id"), (_key("ASIA", 12), "aws-key-id"),
    ("+" + _key(), "aws-key-id"), ('"' + _key("ASIA") + '"', "aws-key-id"),
    ("# " + _key() + " # verified-generic", "aws-key-id"),
    (_url(), "url-credential"), (_url("one:two"), "url-credential"),
    (_url("one%3Atwo"), "url-credential"), (_url(user="", scheme="redis"), "url-credential"),
    (_url(scheme="git+ssh"), "url-credential"),
])
def test_approved_shapes_have_value_free_findings(text, category):
    findings = sanitize._check_credentials(SimpleNamespace(added_lines=[("opaque-object", 7, text)]))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "secret" and finding.severity == "block"
    assert finding.scanner == category
    assert finding.message == "Credential shape detected" and finding.detail is None
    assert (finding.file, finding.line) == ("opaque-object", 7)


@pytest.mark.parametrize("text", [
    _key(length=11), _key("asia"), "x" + _key(), "_" + _key(), _key() + "x",
    "travel across ASIA", _url(password=""),
    "https://example.invalid:8443/path", "https://example.invalid:8443?q=@other",
    "https://example.invalid:8443#@other", "https://example.invalid/path:private@other",
    "git@example.invalid:owner/repo", "fixture:private@example.invalid",
    'API_KEY = os.environ["API_KEY"]', 'password="randomalphanumericlongliteral"',
])
def test_noncredential_shapes_are_not_promoted_to_hard_blocks(text):
    assert sanitize._check_credentials(SimpleNamespace(added_lines=[("opaque", 1, text)])) == []


def test_first_confirmation_and_default_population():
    parsed = SimpleNamespace(added_lines=[("opaque-a", 1, _key()), ("opaque-b", 2, _key("ASIA"))])
    assert len(sanitize._check_credentials(parsed)) == 2
    assert len(sanitize._check_credentials(parsed, stop_after_first=True)) == 1
    assert sanitize._check_credentials(SimpleNamespace(added_lines=[])) == []
