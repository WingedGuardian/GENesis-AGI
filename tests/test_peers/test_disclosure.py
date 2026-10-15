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
