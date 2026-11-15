"""Tests for the output content scanner."""

import pytest

from genesis.security.output_scanner import scan_outbound

# --- Safe content ---


def test_safe_plain_text():
    result = scan_outbound("Thanks for your interest in Genesis! Check out the repo.")
    assert result.safe is True
    assert result.detected == []
    assert result.risk_level == "none"


def test_safe_technology_mentions():
    """General technology names should NOT be flagged."""
    result = scan_outbound(
        "Genesis is built on Python and uses Claude as its reasoning engine. "
        "It runs as a systemd service and stores data in SQLite."
    )
    assert result.safe is True


def test_safe_public_url():
    result = scan_outbound("Check out https://github.com/WingedGuardian/GENesis-AGI")
    assert result.safe is True


# --- API key detection ---


def test_detects_openai_key():
    result = scan_outbound("My key is sk-abc123def456ghi789jkl012mno")
    assert not result.safe
    assert "api_key_openai" in result.detected
    assert result.risk_level == "high"


def test_detects_anthropic_key():
    result = scan_outbound("Use sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    assert not result.safe
    assert "api_key_anthropic" in result.detected
    assert result.risk_level == "high"


def test_detects_groq_key():
    result = scan_outbound("Here's the key: gsk_abcdefghijklmnopqrstuvwxyz")
    assert not result.safe
    assert "api_key_groq" in result.detected


# --- Credential patterns ---


def test_detects_credential_assignment():
    result = scan_outbound("Set password=MyS3cretP@ssw0rd123 in config")
    assert not result.safe
    assert "credential_assignment" in result.detected
    assert result.risk_level == "medium"  # Not critical — prone to false positives


def test_detects_env_variable():
    result = scan_outbound("export DEEPINFRA_API_KEY=di_abc123456789")
    assert not result.safe
    assert "env_variable_secret" in result.detected


# --- IP addresses ---


def test_detects_rfc1918_ip():
    result = scan_outbound("Connect to 192.168.1.100 for the dashboard")
    assert not result.safe
    assert "rfc1918_ip" in result.detected
    assert result.risk_level == "medium"


def test_ignores_public_ip():
    """Public IPs are not flagged — only RFC 1918 private ranges."""
    result = scan_outbound("Our website is at 93.184.216.34")
    assert result.safe is True


# --- File paths ---


def test_detects_internal_file_path():
    result = scan_outbound("The config is at ~/.genesis/config/genesis.yaml")
    assert not result.safe
    assert "internal_file_path" in result.detected


def test_detects_home_path():
    result = scan_outbound("Check /home/ubuntu/genesis/data/genesis.db")
    assert not result.safe
    assert "internal_file_path" in result.detected


# --- Localhost ---


def test_detects_localhost_port():
    result = scan_outbound("Qdrant runs on localhost:6333")
    assert not result.safe
    assert "localhost_port" in result.detected


# --- Multiple patterns ---


def test_multiple_patterns_detected():
    result = scan_outbound(
        "Connect to 192.168.1.100 and use sk-ant-api03-testkey123456789xyz "
        "with password=admin123456"
    )
    assert not result.safe
    assert len(result.detected) >= 3
    assert result.risk_level == "high"


# --- Modern key shapes the shipped [a-zA-Z0-9]{20,} charset MISSED ---
# Regression guard: every one of these returned safe=True before the fix
# because the prefix embeds '-'/'_' that the old charset excluded.


@pytest.mark.parametrize(
    "key",
    [
        "sk-proj-abc_defGHI-jklMNO123456789xyz",  # pragma: allowlist secret
        "sk-svcacct-abcDEF123456_ghiJKL789mno",  # pragma: allowlist secret
        "sk-admin-abcDEF123456789ghiJKL0mnop",  # pragma: allowlist secret
        "sk-or-v1-0123456789abcdef0123456789abcdef",  # pragma: allowlist secret
        "sk-test_abc123XYZ7890defghijk",  # pragma: allowlist secret
    ],
)
def test_detects_modern_openai_family(key):
    result = scan_outbound(f"here it is: {key}")
    assert not result.safe
    assert "api_key_openai" in result.detected
    assert result.risk_level == "high"


def test_detects_anthropic_key_with_underscore():
    # Real Anthropic key bodies contain underscores — the old charset stopped
    # at the first '_' and could truncate below the length floor.
    key = "sk-ant-api03-abc_def-ghijklmnopqrstuvwxyz012345"  # pragma: allowlist secret
    result = scan_outbound(f"key: {key}")
    assert not result.safe
    assert "api_key_anthropic" in result.detected
    assert result.risk_level == "high"


@pytest.mark.parametrize(
    "token",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",  # pragma: allowlist secret
        "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",  # pragma: allowlist secret
        "ghs_0123456789abcdefghijklmnopqrstuvwxyz",  # pragma: allowlist secret
        "ghu_abcdEFGH1234567890ijklMNOP567890qrst",  # pragma: allowlist secret
        "ghr_zyxwvutsrqponmlkjihgfedcba9876543210",  # pragma: allowlist secret
        # fine-grained: github_pat_ + 22 + '_' + 59 (realistic 82-char body)
        "github_pat_11ABCDEFGH0123456789AB_cdefghijklmnopqrstuvwxyz0123456789ABCDEFG",  # pragma: allowlist secret
    ],
)
def test_detects_github_tokens(token):
    result = scan_outbound(f"token = {token}")
    assert not result.safe
    assert "api_key_github" in result.detected
    assert result.risk_level == "high"


# --- False-positive guardrails ---
# Benign strings that superficially resemble a key prefix. The length floors
# (sk-* keeps 20; GitHub tracks the real 36/82 bodies) and the exact GitHub
# prefix set keep these safe — real keys are far longer. The ghi_/short-ghp_
# cases guard the specific false positive Codex flagged (a non-token
# gh[a-z]_ prefix or a too-short body must not quarantine).


@pytest.mark.parametrize(
    "text",
    [
        "a risk-averse mindset pays off",
        "let's have a desk-side chat about it",
        "the SK-1024 model is deprecated",
        "join the ask-me-anything session",
        "see the sk-learn-pipeline notes",
        "the sk-marketing-plan-q3 launch is set",
        "query the ghi_data_warehouse_table_name view",
        "ugh_oh that was a rough deploy",
        "the ghi_abcdefghijklmnopqrstuv identifier",  # not a real gh prefix
        "commit ghp_abc123 is too short to be a token",
    ],
)
def test_benign_lookalikes_not_flagged(text):
    result = scan_outbound(text)
    assert result.safe is True
    assert result.detected == []


_TEST_CARD = "4242" * 4  # Stripe's public test number, built from parts


@pytest.mark.parametrize(
    "text",
    [
        f"Paid with {_TEST_CARD} today",
        f"card {_TEST_CARD[:4]} {_TEST_CARD[4:8]} {_TEST_CARD[8:12]} {_TEST_CARD[12:]}",
    ],
    ids=["unbroken", "grouped"],
)
def test_payment_card_is_critical(text):
    result = scan_outbound(text)
    assert not result.safe
    assert "payment_card" in result.detected
    assert result.risk_level == "high"


def test_card_shaped_but_not_a_card_is_safe():
    # Luhn-failing, decimal fraction, and an epoch timestamp.
    bad_luhn = _TEST_CARD[:-1] + "3"
    for text in (f"order {bad_luhn}", f"score 0.{_TEST_CARD}", "ts 1728600000000"):
        assert scan_outbound(text).safe, text


def test_redact_credentials_cuts_secrets_and_keeps_locations():
    from genesis.security.output_scanner import redact_credentials

    key = "sk-" + "a" * 30
    text = (
        f"use {key} from /home/someone/app on localhost:8080\n"
        + "DB_"
        + "PASSWORD=hunter2hunter2\n"
        + '{"api_key": "abc\\"def", "name": "x"}'
    )
    out, found = redact_credentials(text)
    assert key not in out and "hunter2" not in out and 'abc\\"def' not in out
    assert "/home/someone/app" in out and "localhost:8080" in out
    assert '"name": "x"' in out
    assert {"api_key_openai", "json_credential"} <= set(found)
    assert {"credential_assignment", "env_variable_secret"} & set(found)
    assert scan_outbound(out).detected == ["internal_file_path", "localhost_port"]


def test_redact_credentials_leaves_clean_text_alone():
    from genesis.security.output_scanner import redact_credentials

    assert redact_credentials("nothing to see") == ("nothing to see", [])


@pytest.mark.parametrize(
    "text",
    [
        'password: "abcdefgh!TAILSECRET"',
        "secret=abcdefgh\u00e9TAILSECRET",
        "token: abcdefgh%7ETAILSECRET",
        "api_key = abcdefgh/ok.TAILSECRET",
    ],
)
def test_redaction_takes_the_whole_credential_token(text):
    from genesis.security.output_scanner import redact_credentials

    out, found = redact_credentials(text)
    assert "TAILSECRET" not in out
    assert found == ["credential_assignment"]


def test_json_member_redaction_is_linear_on_hostile_text():
    import time

    from genesis.security.output_scanner import redact_credentials

    # Quadratic before: a long quote-free keyword run closed by a quote with no
    # colon took 1.2 s at 40 KB. Linear is well under a second at 400 KB.
    hostile = '"' + "token password " * 27_000 + '" x'
    start = time.perf_counter()
    redact_credentials(hostile)
    assert time.perf_counter() - start < 5


def test_json_member_with_an_ordinary_key_is_kept():
    from genesis.security.output_scanner import redact_credentials

    text = '{"name": "value", "api_key": "abc", "count": "3"}'
    out, _ = redact_credentials(text)
    assert out == '{"name": "value", "api_key": "[redacted:json_credential]", "count": "3"}'


@pytest.mark.parametrize(
    "text",
    [
        'password: "abcdefgh,TAILSECRET"',
        "password: 'abcdefgh TAILSECRET'",
        'API_TOKEN="abc def TAILSECRET"',
        "DB_SECRET_KEY='xx yy TAILSECRET'",
    ],
)
def test_quoted_credential_values_are_redacted_to_the_closing_quote(text):
    from genesis.security.output_scanner import redact_credentials

    out, found = redact_credentials(text)
    assert "TAILSECRET" not in out
    assert found


def test_json_keys_that_only_contain_a_credential_word_keep_their_values():
    from genesis.security.output_scanner import redact_credentials

    text = (
        '{"token_count": "1200", "token_type": "Bearer", '
        '"tokenizer": "cl100k", "password_required": "false"}'
    )
    assert redact_credentials(text) == (text, [])


@pytest.mark.parametrize("key", ["access_token", "db_password", "apiKey", "client_secret", "token"])
def test_json_keys_named_as_credentials_are_redacted(key):
    from genesis.security.output_scanner import redact_credentials

    out, found = redact_credentials('{"' + key + '": "TAILSECRETvalue"}')
    assert "TAILSECRET" not in out
    assert found == ["json_credential"]


@pytest.mark.parametrize(
    "key",
    [
        "secret_key",
        "api_secret_key",
        "secrets",
        "passwords",
        "api_keys",
        "password_hash",
        "token_value",
        "token ",
        "accessToken",
        "API-KEY",
        "x.password",
        "privateKey",
        "credentials",
    ],
)
def test_keys_that_name_a_credential_in_any_form_are_redacted(key):
    from genesis.security.output_scanner import redact_credentials

    out, found = redact_credentials('{"' + key + '": "TAILSECRETvalue"}')
    assert "TAILSECRET" not in out
    assert found == ["json_credential"]


@pytest.mark.parametrize(
    "key", ["max_tokens", "password_policy", "token_count", "tokenizer", "token_type"]
)
def test_keys_about_a_credential_keep_their_values(key):
    from genesis.security.output_scanner import redact_credentials

    text = '{"' + key + '": "keep-me"}'
    assert redact_credentials(text) == (text, [])


def test_mixed_quotes_cannot_leave_a_tail():
    from genesis.security.output_scanner import redact_credentials

    out, _ = redact_credentials('password = "abcdefgh\'TAILSECRET" and more')
    assert "TAILSECRET" not in out


def test_an_assignment_is_cut_to_the_end_of_its_line_only():
    from genesis.security.output_scanner import redact_credentials

    out, _ = redact_credentials("a\npassword: abcdefgh!TAILSECRET\nnext line stays")
    assert out == "a\n[redacted:credential_assignment]\nnext line stays"


@pytest.mark.parametrize(
    "key",
    [
        "passwd",
        "pwd",
        "password2",
        "passphrase",
        "access_key",
        "Authorization",
        "token_type_value",
        "password_name",
        "token_id",
        "token_url",
    ],
)
def test_more_credential_key_shapes_are_redacted(key):
    from genesis.security.output_scanner import redact_credentials

    out, found = redact_credentials('{"' + key + '": "TAILSECRETvalue"}')
    assert "TAILSECRET" not in out
    assert found == ["json_credential"]


@pytest.mark.parametrize(
    "text",
    ["sk-" + "a" * 30 + ".TAILSECRETvalue", "gsk_" + "b" * 30 + "-TAILSECRET"],
)
def test_a_key_shape_takes_its_whole_token(text):
    from genesis.security.output_scanner import redact_credentials

    out, _ = redact_credentials(f"use {text} now")
    assert "TAILSECRET" not in out
    assert out.endswith(" now")


def test_an_env_assignment_takes_a_value_wrapped_onto_the_next_line():
    from genesis.security.output_scanner import redact_credentials

    out, found = redact_credentials('API_TOKEN=\n"abc def TAILSECRET"\nnext line stays')
    assert "TAILSECRET" not in out
    assert out.endswith("\nnext line stays")
    assert found == ["env_variable_secret"]


@pytest.mark.parametrize("prefix", ["sk-", "gsk_", "ghp_"])
def test_a_key_shape_inside_a_json_string_keeps_the_closing_quote(prefix):
    import json

    from genesis.security.output_scanner import redact_credentials

    out, _ = redact_credentials('{"note": "use ' + prefix + "a" * 36 + '", "answer": "OK"}')
    assert json.loads(out)["answer"] == "OK"
    assert "a" * 20 not in out


@pytest.mark.parametrize("sep", ["\n", "\r\n", "\n\n", "\n \n"])
def test_a_value_below_a_name_the_cut_swallowed_goes_with_it(sep):
    from genesis.security.output_scanner import redact_credentials

    out, _ = redact_credentials("note password: abcdefgh123 API_TOKEN=" + sep + "TAILSECRET\nkept")
    assert "TAILSECRET" not in out
    assert out.endswith("\nkept")


def test_a_flagged_line_ending_in_a_colon_keeps_the_lines_below():
    from genesis.security.output_scanner import redact_credentials

    text = "def connect(host, password=DEFAULT_PASSWORD):\n    open(host)\n    return 1"
    out, _ = redact_credentials(text)
    assert out == "def connect(host, [redacted:credential_assignment]\n    open(host)\n    return 1"


def test_a_crlf_line_ending_survives_the_cut():
    from genesis.security.output_scanner import redact_credentials

    out, _ = redact_credentials("a\r\npassword: abcdefgh123 tail\r\nb")
    assert out == "a\r\n[redacted:credential_assignment]\r\nb"


def test_assignment_redaction_is_linear_on_hostile_text():
    import time

    from genesis.security.output_scanner import redact_credentials

    hostile = "password: abcdefgh1 " * 100_000 + ": = " * 200_000
    start = time.perf_counter()
    redact_credentials(hostile)
    assert time.perf_counter() - start < 2


# --- Shapes adopted from the shared table (genesis.security.credential_shapes) ---
# Every credential value is synthetic and built from parts. The one exception is
# _INHERITED_OVER_MATCHES below: benign literals that match a shape on purpose.

_JWT = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 43
_NVAPI = "nvapi-" + "a" * 40
_DISCORD_HOOK = "https://discord" + ".com/api/webhooks/" + "1" * 18 + "/" + "G" * 60
_BOT_TOKEN = "1234567890:" + "H" * 35

# name -> (text carrying the credential, the part that must be gone afterwards)
_SHARED_VALUES = {
    "gitlab": ("glpat-" + "E1b2" * 5,) * 2,
    "nvidia": (_NVAPI,) * 2,
    "tavily": ("tvly-" + "b" * 30,) * 2,
    "firecrawl": ("fc-" + "f1" * 16,) * 2,
    "huggingface": ("hf_" + "C" * 34,) * 2,
    "xai": ("xai-" + "D" * 40,) * 2,
    "slack_bot": ("xoxb-" + "2" * 24,) * 2,
    "slack_user": ("xoxp-" + "3" * 24,) * 2,
    "aws_access_key": ("AKIA" + "K" * 16,) * 2,
    "google": ("AIza" + "m" * 35,) * 2,
    "deepinfra": ("di-" + "n" * 30,) * 2,
    "jwt": (_JWT,) * 2,
    "cerebras": ("csk-" + "i" * 40,) * 2,
    "stripe_secret": ("sk_live_" + "j" * 24,) * 2,
    "stripe_restricted": ("rk_live_" + "q" * 24,) * 2,
    "aws_sts": ("ASIA" + "L" * 16,) * 2,
    "npm": ("npm_" + "o" * 36,) * 2,
    "pypi": ("pypi-" + "p" * 50,) * 2,
    "discord_webhook": (_DISCORD_HOOK, "G" * 60),
    "telegram_bot_token": (_BOT_TOKEN,) * 2,
}
# The form a bot API URL carries: the id follows the letters "bot".
_BOT_URL = "https://api.telegram" + ".org/bot" + _BOT_TOKEN + "/sendMessage"

_CONTEXTS = {
    "bare": "{}",
    "prose": "the run printed {} and then stopped",
    "trailing_dot": "it printed {}.",
    "bang_rest": "it printed {}!rest",
    "parentheses": "the value ({}) was logged",
    "double_quotes": 'it printed "{}" today',
    "single_quotes": "it printed '{}' today",
    "json_string": '{{"note": "saw {}", "answer": "OK"}}',
    "json_array": '["first", "{}", "last"]',
    "comma_other": "{},other",
    "own_line": "before\n{}\nafter",
    "diff_removed": "-{}",
    "diff_added": "+{}",
    "value_equals": "value={}",
    "angle_brackets": "<{}>",
    "backtick": "`{}`",
    "url_query": "https://example.org/cb?t={}",
}


def _shared_cases():
    for name, (value, secret) in _SHARED_VALUES.items():
        for context, template in _CONTEXTS.items():
            yield pytest.param(name, template.format(value), secret, id=f"{name}-{context}")
    for context, template in _CONTEXTS.items():
        yield pytest.param(
            "telegram_bot_token", template.format(_BOT_URL), _BOT_TOKEN, id=f"bot_url-{context}"
        )


def test_the_shared_value_table_covers_every_adopted_shape():
    from genesis.security import output_scanner

    assert {f"credential_{name}" for name in _SHARED_VALUES} == output_scanner._SHARED_NAMES


@pytest.mark.parametrize("name,text,secret", _shared_cases())
def test_shared_shapes_are_detected_redacted_and_clean(name, text, secret):
    from genesis.security.output_scanner import (
        LOCATION_FINDINGS,
        redact_credentials,
        scan_outbound_normalised,
    )

    result = scan_outbound(text)
    assert set(result.detected) == {f"credential_{name}"}
    assert result.risk_level == "high"
    out, found = redact_credentials(text)
    assert secret not in out and secret[-8:] not in out
    assert f"[redacted:credential_{name}]" in out
    assert found == [f"credential_{name}"]
    assert scan_outbound_normalised(out) - LOCATION_FINDINGS == set()


@pytest.mark.parametrize("name", ["nvidia", "jwt", "discord_webhook", "telegram_bot_token"])
def test_a_shared_shape_takes_its_whole_token(name):
    """A body character outside the shape's class ends the match, not the token:
    whatever follows up to whitespace or a quote goes with it."""
    from genesis.security.output_scanner import redact_credentials

    value = _SHARED_VALUES[name][0]
    out, _ = redact_credentials(f"use {value}%7ETAILSECRETvalue now")
    assert out == f"use [redacted:credential_{name}] now"
    out, _ = redact_credentials('{"note": "use ' + value + '!TAILSECRET", "answer": "OK"}')
    assert out == '{"note": "use [redacted:credential_' + name + ']", "answer": "OK"}'


def test_authorization_bearer_jwt_is_detected_and_redacted():
    import base64
    import json

    from genesis.security.output_scanner import redact_credentials, scan_outbound_normalised

    def segment(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    # A structurally real token, encoded at run time: header, claims, signature.
    jwt = ".".join([segment({"alg": "HS256", "typ": "JWT"}), segment({"sub": "x"}), "S" * 43])
    assert jwt.startswith("eyJ")  # guard: the fixture has the shape it claims
    text = "Authorization: Bearer " + jwt
    result = scan_outbound(text)
    assert not result.safe
    assert result.detected == ["credential_jwt"]
    assert result.risk_level == "high"
    out, found = redact_credentials(text)
    assert out == "Authorization: Bearer [redacted:credential_jwt]"
    assert found == ["credential_jwt"]
    assert scan_outbound_normalised(out) == set()


def test_every_shared_shape_is_adopted_or_locally_owned():
    """A new entry in the shared table cannot be silently skipped here."""
    from genesis.security import credential_shapes, output_scanner

    table = {name for name, _ in credential_shapes.KEY_PREFIX_SHAPES}
    table |= {name for name, _ in credential_shapes.STRUCTURED_SHAPES}
    assert table > output_scanner._LOCAL_FAMILIES
    adopted = table - output_scanner._LOCAL_FAMILIES
    assert {f"credential_{name}" for name in adopted} == output_scanner._SHARED_NAMES
    assert output_scanner.pattern_names() >= output_scanner._SHARED_NAMES
    assert output_scanner._CRITICAL_PATTERNS >= output_scanner._SHARED_NAMES


@pytest.mark.parametrize(
    "value,local_name",
    [
        ("gsk_" + "R" * 30, "api_key_groq"),
        ("sk-" + "s" * 30, "api_key_openai"),
        ("sk-ant-" + "t" * 30, "api_key_anthropic"),
        ("ghp_" + "U" * 36, "api_key_github"),
        ("github_pat_" + "V" * 60, "api_key_github"),
    ],
    ids=["groq", "openai", "anthropic", "github", "github_fine_grained"],
)
def test_locally_owned_families_report_only_their_local_name(value, local_name):
    from genesis.security import output_scanner

    result = scan_outbound(f"here it is: {value}")
    assert local_name in result.detected
    assert not set(result.detected) & output_scanner._SHARED_NAMES
    _, found = output_scanner.redact_credentials(f"here it is: {value}")
    assert not set(found) & output_scanner._SHARED_NAMES


def test_a_shared_shape_split_by_a_control_character_is_found_in_preflight():
    from genesis.security.output_scanner import scan_diagnostic_controls

    assert scan_diagnostic_controls("word\x00hf_" + "A" * 30) == {"credential_huggingface"}


# False-positive controls: one or more per adopted shape. Each sits just outside
# its shape (one character under the length floor, a character the body class
# excludes, or an ordinary word that starts with the prefix).
_LOOKALIKES = {
    "gitlab_short": "glpat-short",
    "gitlab_under_floor": "glpat-" + "a" * 14,
    "nvidia_short": "set nvapi-key-here first",
    "nvidia_under_floor": "nvapi-" + "a" * 19,
    "tavily_client": "install the tvly-python client",
    "tavily_under_floor": "tvly-" + "a" * 19,
    "firecrawl_word": "fc-barcelona won again",
    "firecrawl_under_floor": "fc-" + "a" * 23,
    "huggingface_function": "call hf_hub_download(repo_id)",
    "huggingface_under_floor": "hf_" + "a" * 19,
    "xai_model": "the xai-grok-beta model",
    "xai_under_floor": "xai-" + "a" * 19,
    "slack_bot_placeholder": "xoxb-your-token",
    "slack_user_under_floor": "xoxp-" + "1" * 19,
    "aws_short": "AKIA12345678",
    "aws_lowercase_body": "AKIA" + "a" * 16,
    "aws_sts_word": "ASIAPACIFIC",
    "aws_sts_prose": "shipping to ASIA next week",
    "google_under_floor": "AIza" + "x" * 29,
    "deepinfra_inside_word": "audi-a4-quattro-2026-model-year",
    "deepinfra_inside_long_slug": "audi-a4quattro2026modelyearedition",
    "deepinfra_under_floor": "di-" + "a" * 19,
    "deepinfra_prose": "the di-electric constant",
    "jwt_short_segments": "eyJabc.def.ghi",
    "jwt_two_segments": "eyJ" + "a" * 20 + "." + "b" * 20,
    "jwt_spaced": "eyJ" + "a" * 20 + " . " + "b" * 20 + " . " + "c" * 20,
    "cerebras_short": "csk-test",
    "cerebras_under_floor": "csk-" + "a" * 19,
    "stripe_plan_doc": "the sk_live_migration plan doc",
    "stripe_test_mode": "sk_test_" + "a" * 24,
    "stripe_restricted_under_floor": "rk_live_" + "a" * 15,
    "npm_env_var": "npm_config_registry",
    "npm_under_floor": "npm_" + "a" * 29,
    "pypi_action": "pypi-publish-action",
    "pypi_under_floor": "pypi-" + "a" * 39,
    "discord_invite": "https://discord" + ".gg/abcdefgh",
    "discord_channel": "https://discord" + ".com/channels/" + "1" * 18 + "/" + "2" * 18,
    "discord_hook_short_token": "https://discord"
    + ".com/api/webhooks/"
    + "1" * 18
    + "/"
    + "G" * 39,
    "discord_hook_short_id": "https://discord" + ".com/api/webhooks/" + "1" * 14 + "/" + "G" * 60,
    "discord_other_host": "https://example.org/api/webhooks/" + "1" * 18 + "/" + "G" * 60,
    "telegram_clock": "12:30:45",
    "telegram_short_secret": "123456:abc",
    "telegram_short_id": "12345:" + "H" * 35,
    "telegram_under_floor": "1234567890:" + "H" * 29,
    "telegram_chat_prose": "chat 1234567890: hello there, how are you doing today my friend",
    "telegram_digest": "sha256:" + "0123456789abcdef" * 4,
    "telegram_timestamp": "at 20261112211900: the run started and finished without trouble",
    "authorization_placeholder": "Authorization: Bearer <token>",
    "authorization_variable": "Authorization: Bearer ${API_TOKEN}",
    "bearer_prose": "the bearer of bad news",
}


@pytest.mark.parametrize("text", list(_LOOKALIKES.values()), ids=list(_LOOKALIKES))
def test_shared_shapes_do_not_flag_lookalikes(text):
    from genesis.security.output_scanner import redact_credentials

    result = scan_outbound(text)
    assert result.safe is True
    assert result.detected == []
    assert redact_credentials(text) == (text, [])


def test_a_lookalike_beside_a_real_shape_is_not_reported_or_cut():
    """The one-pass gate admits the whole text once any shape is present, so each
    per-shape pattern must still reject the lookalikes on its own."""
    from genesis.security.output_scanner import redact_credentials

    slug = _LOOKALIKES["deepinfra_inside_long_slug"]
    text = f"key {_NVAPI} for the {slug} listing at {_LOOKALIKES['telegram_clock']}"
    assert scan_outbound(text).detected == ["credential_nvidia"]
    out, found = redact_credentials(text)
    assert out == f"key [redacted:credential_nvidia] for the {slug} listing at 12:30:45"
    assert found == ["credential_nvidia"]


# The shared table is recall-first: these benign strings ARE flagged. They are
# pinned so the cost is deliberate and a narrowing of the table shows up here.
_INHERITED_OVER_MATCHES = {
    "gitlab_slug": ("glpat-staging-eu-west1", "gitlab"),
    "dotted_filename": ("eyJournalfile.name.extension123", "jwt"),
    "date_and_hash": ("20261112:" + "deadbeef" * 4, "telegram_bot_token"),
    "epoch_and_hash": ("1762981140:" + "deadbeef" * 4, "telegram_bot_token"),
    "region_word": ("ASIAPACIFICREGION1", "aws_sts"),
    "pypi_slug": ("pypi-how-to-publish-your-python-package-to-the-index-today", "pypi"),
    "tavily_slug": ("tvly-search-results-for-the-query-here", "tavily"),
    "nvidia_slug": ("nvapi-compatible-endpoint-openai-style", "nvidia"),
}


@pytest.mark.parametrize(
    "text,name", list(_INHERITED_OVER_MATCHES.values()), ids=list(_INHERITED_OVER_MATCHES)
)
def test_inherited_over_matches_are_pinned(text, name):
    assert scan_outbound(text).detected == [f"credential_{name}"]


class _MustNotRun:
    """Stands in for a per-shape pattern that the one-pass gate must skip."""

    def search(self, _content):
        raise AssertionError("per-shape detector ran")

    def subn(self, _repl, _content):
        raise AssertionError("per-shape redactor ran")


def test_text_without_a_shared_shape_never_runs_the_per_shape_passes(monkeypatch):
    """The cost bound, pinned without a clock: text the composed table patterns
    do not match costs two passes, not one per shape."""
    from genesis.security import output_scanner

    monkeypatch.setattr(output_scanner, "_SHARED_PATTERNS", (("credential_spy", _MustNotRun()),))
    monkeypatch.setattr(output_scanner, "_SHARED_REDACTORS", {"credential_spy": _MustNotRun()})
    clean = "an ordinary line about a deploy, a clock at 12:30:45 and hf_hub_download\n" * 200
    assert scan_outbound(clean).safe
    assert output_scanner.redact_credentials(clean) == (clean, [])
    # Guard: the spies are reachable, so the clean run above proved the skip.
    with pytest.raises(AssertionError, match="per-shape detector ran"):
        scan_outbound(clean + _NVAPI)
    with pytest.raises(AssertionError, match="per-shape redactor ran"):
        output_scanner.redact_credentials(clean + _NVAPI)
