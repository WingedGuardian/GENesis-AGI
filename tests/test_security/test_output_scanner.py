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
