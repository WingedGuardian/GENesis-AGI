import json

import pytest

from genesis.peers.disclosure_scan import json_strings_safe


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda s: s,
        lambda s: "prose\n" + s,
        lambda s: "```json\n" + s + "\n```",
        lambda s: "two objects {} " + s,
    ],
)
def test_credentials_in_json_and_prose_are_withheld(wrapper):
    data = {"password": "".join(["synthetic", "credential"])}
    assert not json_strings_safe(wrapper(json.dumps(data)))
    escaped = (
        '{"' + "".join(f"\\u{ord(c):04x}" for c in "password") + '":"' + data["password"] + '"}'
    )
    assert not json_strings_safe(wrapper(escaped))


@pytest.mark.parametrize(
    "value",
    [
        "ordinary [notes]",
        {"answer": "independent reasoning"},
        'prose {"answer":"reasoning"}',
        "Use {x} and [Genesis](https://example.org)",
        "bounded [remainder omitted]",
    ],
)
def test_plain_safe_and_embedded_json_are_supported(value):
    assert json_strings_safe(value)


@pytest.mark.parametrize(
    "value",
    [
        'see "\\/home\\/synthetic\\/notes"',  # quoted JSON string token in prose
        "see \\\\/home\\\\/synthetic\\\\/notes",  # escaped twice
        "host 10\\u002e0\\u002e0\\u002e1 answered",  # bare escapes in prose
        {"API\\u005fKEY": "syntheticvalue1"},  # escaped mapping key
    ],
)
def test_escaped_disclosures_in_prose_are_withheld(value):
    assert not json_strings_safe(value)


def test_ordinary_escapes_in_prose_stay_disclosable():
    assert json_strings_safe('line one\\nline two \\"quoted\\" caf\\u00e9')
    assert json_strings_safe("C:\\Users\\Public\\notes")
    # Decoding can leave a truncated JSON fragment; that is prose, not refusal.
    assert json_strings_safe(
        'The log showed {\\"event\\": \\"start\\", \\"n\\": 1 and then stopped.'
    )
    assert json_strings_safe('match \\d+\\.\\d+ then [\\"a\\" or')


def test_escaped_json_object_in_prose_is_still_parsed():
    secret = "".join(["synthetic", "credential"])
    assert not json_strings_safe('logged {\\"password\\": \\"' + secret + '\\"} today')


def test_nested_decoded_compound_keys_malformed_cycles_and_work():
    assert not json_strings_safe({json.dumps({"safe": "key"}): "value"})
    assert not json_strings_safe('prose {"pass\\u0077ord":"unfinished')
    cycle = []
    cycle.append(cycle)
    assert not json_strings_safe(cycle)
    assert not json_strings_safe("a" * (2 * 1024 * 1024 + 1))
    assert not json_strings_safe(
        {"answer": json.dumps({"password": "".join(["synthetic", "credential"])})}
    )


def test_allow_tolerates_only_the_named_patterns():
    private_ip = ".".join(("192", "168", "1", "5"))  # built, so no literal address ships
    located = f"the file is /home/someone/notes on {private_ip}"
    assert not json_strings_safe(located)
    assert json_strings_safe(located, allow={"internal_file_path", "rfc1918_ip"})
    secret = located + " and " + "API_" + "TOKEN=" + "syntheticvalue123"
    assert not json_strings_safe(secret, allow={"internal_file_path", "rfc1918_ip"})
    keyed = {"note": located, "password": "syntheticcredential"}
    assert not json_strings_safe(keyed, allow={"internal_file_path", "rfc1918_ip"})


def test_reply_allow_names_real_scanner_patterns():
    from genesis.peers.results import REPLY_ALLOW
    from genesis.security.output_scanner import pattern_names

    assert pattern_names() >= REPLY_ALLOW
    assert "payment_card" not in REPLY_ALLOW
