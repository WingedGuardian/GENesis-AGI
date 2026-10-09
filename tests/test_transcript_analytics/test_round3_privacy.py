"""Round-three scoped privacy and structural classification regressions."""

import json

import pytest

from genesis.transcript_analytics import scrub
from genesis.transcript_analytics.classify import classify


@pytest.mark.parametrize("label", ["GH_TOKEN", "SERVICE_AUTH", "password", "api_key"])
def test_exact_helper_redacts_short_structured_values(label):
    text, failed = scrub.scrub_text(json.dumps({label: "x", "diagnostic": "keep"}))
    assert not failed
    assert json.loads(text)[label] == "[REDACTED]"


@pytest.mark.parametrize(
    "raw", ['prefix {"value":"\\u0067hp_escaped",', '"GH_TOKEN":"x"', '{"x":1,"x":2}']
)
def test_recognized_malformed_or_duplicate_withheld(raw):
    assert scrub.scrub_text(raw) == (None, True)


@pytest.mark.parametrize(
    "raw",
    ['echo "hello world"', "cat <<EOF\n{ordinary shell}\nEOF", "[1, invalid]", '"unterminated'],
)
def test_accepted_residue_and_ordinary_commands(raw):
    assert scrub.scrub_text(raw) == (raw, False)


def test_global_decode_budget_shared_across_branches():
    assert scrub.scrub_json([json.dumps("ok")] * 65) == (None, True)


def test_sanitized_key_collision_withheld(monkeypatch):
    monkeypatch.setattr(scrub, "_scrub", lambda text: "same" if text in ("a", "b") else text)
    assert scrub.scrub_json({"a": 1, "b": 2}) == (None, True)


@pytest.mark.parametrize(
    "raw", ['{ "error" : "bad" }', '{"other":1,"error":"bad"}', '{\n"error":"bad"\n}']
)
@pytest.mark.parametrize("flag", [True, False, None])
def test_structural_error_respects_explicit_success(raw, flag):
    assert classify(raw, is_error=flag, denial_kind=None) == (
        (None, None) if flag is False else ("mcp_error", "flag" if flag else "text")
    )


def test_custom_title_is_retained(tmp_path):
    from genesis.transcript_analytics.extract import extract_source

    path = tmp_path / "source.jsonl"
    path.write_text(
        json.dumps({"type": "custom-title", "sessionId": "s", "customTitle": "useful title"}) + "\n"
    )
    result = extract_source(path, path.name)
    assert result.tables["session_meta"][0]["kind"] == "custom-title"
    assert result.tables["session_meta"][0]["value"] == "useful title"


@pytest.mark.parametrize(
    "raw", ['{"error":null}', '{"error":42}', '{"error":"bad",', 'prefix {"error":"bad"}']
)
def test_structural_error_needs_complete_object_with_string(raw):
    assert classify(raw, is_error=None, denial_kind=None) == (None, None)


@pytest.mark.parametrize(
    "value", ["password: abcdef", "GH_TOKEN=abcdefghi", "api_key: opaquevalue123456789"]
)
@pytest.mark.parametrize("shape", ["object", "array", "scalar", "serialized", "nested"])
@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_rendered_json_survives_prose_pass_and_repeat(value, shape, position):
    members = [("before", "keep"), ("note", value), ("after", "keep")]
    if position == "first":
        members = members[1:] + members[:1]
    elif position == "last":
        members = members[::2] + members[1:2]
    item = dict(members)
    if shape == "array":
        item = [item]
    elif shape == "scalar":
        item = value
    elif shape == "serialized":
        item = json.dumps(item)
    elif shape == "nested":
        item = {"nested": json.dumps([item])}
    raw = json.dumps(item)
    out, failed = scrub.scrub_text(raw)
    assert not failed
    json.loads(out)
    assert "abcdef" not in out and "opaquevalue123456789" not in out
    assert scrub.scrub_text(out) == (out, False)
    surrounded, failed = scrub.scrub_text("prefix " + raw + " suffix")
    assert not failed and surrounded == "prefix " + out + " suffix"


def test_complete_legacy_plaintext_fixture_population():
    from pathlib import Path

    fixture = Path(scrub._path()).parents[2] / "tests/fixtures/credential-label-plaintext.json"
    for raw, expected in json.loads(fixture.read_text()):
        assert scrub.scrub_text(raw) == (expected, False)


@pytest.mark.parametrize("quoted", ['""', '"x"', '"two words"', '"abc\\" def"', '"abc\\tdef"'])
@pytest.mark.parametrize("prefix", ["password: ", "GH_TOKEN=", "api_key: "])
def test_unchanged_quoted_prose_keeps_exact_shared_policy(prefix, quoted):
    raw = prefix + quoted
    assert scrub.scrub_text(raw) == (scrub._scrub(raw), False)


@pytest.mark.parametrize("dense", [False, True])
@pytest.mark.parametrize("normalize", [False, True])
def test_protected_spans_do_not_collide_with_literal_private_use_or_controls(
    dense, normalize, monkeypatch
):
    literal = "".join(map(chr, range(0xE000, 0xF900))) if dense else "\ue000\ue001\ue0000\ue000"
    if normalize:
        actual = scrub._scrub
        monkeypatch.setattr(scrub, "_scrub", lambda text: actual(text.replace("\u200b", "")))
    prefix = scrub._scrub(literal + "\u200b")
    raw = literal + "\u200b" + json.dumps({"note": "password: abcdef"}) + literal
    out, failed = scrub.scrub_text(raw)
    assert not failed and out.startswith(prefix) and out.endswith(literal)
    middle = out[len(prefix) : -len(literal)]
    assert json.loads(middle) == {"note": "password: [REDACTED]"}
    assert scrub.scrub_text(out) == (out, False)


@pytest.mark.parametrize("failure", ["placeholder", "none", "partial-marker"])
def test_sensitive_span_or_protected_pass_failure_withholds(monkeypatch, failure):
    actual = scrub._scrub

    def broken(text):
        if failure == "partial-marker" and "\ue000" in text:
            return text.replace("\ue000", "", 1)
        if failure != "partial-marker" and text.startswith("{"):
            return scrub._PLACEHOLDER if failure == "placeholder" else None
        return actual(text)

    monkeypatch.setattr(scrub, "_scrub", broken)
    assert scrub.scrub_text(json.dumps({"note": "password: abcdef"})) == (None, True)
