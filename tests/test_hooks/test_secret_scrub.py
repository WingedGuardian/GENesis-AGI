"""Unit tests for scripts/hooks/secret_scrub.py — the stdlib secret scrubber.

The scrubber protects hook-captured telemetry (session observations, audit
trails) from persisting secrets, so these lock in BOTH directions: real secret
shapes are redacted, and benign look-alikes are left intact (over-redaction in
telemetry is cheap, but redacting normal env vars / paths would gut the notes).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import secret_scrub as s  # noqa: E402

R = "[REDACTED]"


# ── scrub: real secrets redacted ─────────────────────────────────────────


class TestScrubRedacts:
    def test_github_pat(self):
        assert R in s.scrub("token=ghp_" + "a" * 36)

    def test_openai_key(self):
        assert R in s.scrub("key sk-" + "b" * 30)

    def test_aws_akia(self):
        assert R in s.scrub("AKIA" + "A" * 16)

    def test_google_key(self):
        assert R in s.scrub("AIza" + "c" * 35)

    def test_slack_token(self):
        assert R in s.scrub("xoxb-" + "1" * 25)

    def test_labeled_api_key(self):
        out = s.scrub("api_key: " + "Z" * 20)
        assert R in out and "api_key" in out.lower()

    def test_password_value(self):
        out = s.scrub("password: hunter2secret")
        assert R in out and "hunter2secret" not in out

    def test_passphrase(self):
        assert "correcthorse" not in s.scrub("passphrase = correcthorsebattery")

    def test_env_secret_assignment(self):
        out = s.scrub("export API_KEY=sk-" + "d" * 25)
        assert R in out and "API_KEY" in out

    def test_env_various_secret_keys(self):
        for key in ("AWS_SECRET_ACCESS_KEY", "DB_PASSWORD", "AUTH_TOKEN", "MY_API_SECRET"):
            out = s.scrub(f"{key}=verysecretvalue123")
            assert R in out, f"{key} not redacted"

    def test_token_survives_surrounding_text(self):
        out = s.scrub("before ghp_" + "a" * 36 + " after")
        assert "before" in out and "after" in out and R in out


# ── scrub: benign look-alikes preserved ──────────────────────────────────


class TestScrubPreserves:
    def test_pythonpath(self):
        assert s.scrub("PYTHONPATH=/home/x/src") == "PYTHONPATH=/home/x/src"

    def test_editor_env(self):
        assert s.scrub("EDITOR=/usr/bin/vim") == "EDITOR=/usr/bin/vim"

    def test_ssh_userhost_kept(self):
        # ssh target is a connection detail, not a secret — the hook needs it.
        assert s.scrub("ssh deploy@192.0.2.10") == "ssh deploy@192.0.2.10"

    def test_plain_prose(self):
        txt = "Refactored the retrieval pipeline and fixed the reranker."
        assert s.scrub(txt) == txt

    def test_empty(self):
        assert s.scrub("") == ""


# ── is_secret_path ───────────────────────────────────────────────────────


class TestIsSecretPath:
    def test_dotenv(self):
        assert s.is_secret_path("/app/.env")

    def test_secrets_env(self):
        assert s.is_secret_path("/home/u/.genesis/secrets.env")

    def test_pem(self):
        assert s.is_secret_path("/certs/server.pem")

    def test_ssh_key(self):
        assert s.is_secret_path("/home/u/.ssh/id_ed25519")

    def test_credentials_json(self):
        assert s.is_secret_path("/x/credentials.json")

    def test_name_contains_secret(self):
        assert s.is_secret_path("/x/my_secret_notes.txt")

    def test_python_file_not_secret(self):
        assert not s.is_secret_path("/x/module.py")

    def test_env_example_not_secret(self):
        assert not s.is_secret_path("/x/.env.example")
        assert not s.is_secret_path("/x/secrets.env.sample")

    def test_empty_not_secret(self):
        assert not s.is_secret_path("")


# ── command_touches_secret ───────────────────────────────────────────────


class TestCommandTouchesSecret:
    def test_cat_secrets(self):
        assert s.command_touches_secret("cat ~/.genesis/secrets.env")

    def test_source_dotenv(self):
        assert s.command_touches_secret("source .env && run")

    def test_read_pem(self):
        assert s.command_touches_secret("openssl x509 -in /certs/key.pem")

    def test_plain_ls_not(self):
        assert not s.command_touches_secret("ls -la /home")

    def test_git_status_not(self):
        assert not s.command_touches_secret("git status --short")


# ── Round-2: gaps the adversarial review found ───────────────────────────


class TestReviewFixes:
    def test_aws_credentials_file_and_command_agree(self):
        # S1: the file check and the command check must not diverge.
        assert s.is_secret_path("/home/u/.aws/credentials")
        assert s.command_touches_secret("cat ~/.aws/credentials")

    def test_pipe_to_secret_read(self):
        # N4: a pipe must not hide a secret-file read.
        assert s.command_touches_secret("grep pw .env|cat")

    def test_extra_secret_file_types(self):
        # N3: keystores, npm/pypi rc, kube config, generic .token.
        for p in (
            "/certs/store.p12",
            "/certs/store.pfx",
            "/app/keystore.jks",
            "/home/u/.npmrc",
            "/home/u/.pypirc",
            "/home/u/.kube/config",
            "/run/session.token",
        ):
            assert s.is_secret_path(p), p

    def test_url_embedded_credential_redacted(self):
        # N2: scheme://user:PASSWORD@host
        out = s.scrub("DATABASE_URL=postgres://user:s3cr3tpw@db.host/app")
        assert "s3cr3tpw" not in out and R in out

    def test_host_port_url_not_redacted(self):
        # No userinfo '@' → not a credential URL, leave intact.
        assert s.scrub("http://example.com:8080/path") == "http://example.com:8080/path"

    def test_aws_secret_access_key_label(self):
        out = s.scrub("aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYKEY")
        assert "wJalrXUtnFEMIK7MDENGbPxRfiCYKEY" not in out and R in out

    def test_value_equal_to_label_not_mangled(self):
        # N1: span redaction, not first-occurrence substring replace.
        assert s.scrub("password: password") == "password: [REDACTED]"

    def test_quoted_password_with_spaces_fully_redacted(self):
        # Codex P1: a quoted password/passphrase containing whitespace must be
        # redacted WHOLE, not just up to the first space.
        out = s.scrub('password: "hunter 2 correct horse"')
        assert "hunter" not in out and "horse" not in out and R in out
        # value words chosen NOT to be substrings of the label ("passphrase").
        out2 = s.scrub("passphrase = 'zebra quux mango token'")
        assert "zebra" not in out2 and "mango" not in out2 and R in out2

    def test_env_quoted_multiword_secret_fully_redacted(self):
        # Codex P1 (round 2): the env-assignment path must ALSO redact a quoted
        # multi-word value whole — for secret-hinted keys the password-label
        # pattern does NOT catch, the env pattern is the only line of defence,
        # and its value group used to stop at the first space (tail leaked).
        for line in (
            "MY_SECRET='correct horse battery'",
            "AWS_SECRET_ACCESS_KEY='correct horse battery'",
            "DB_PASSWORD='correct horse battery'",  # no \b between '_' and 'P'
            'API_TOKEN="alpha bravo charlie"',
        ):
            out = s.scrub(line)
            assert R in out, line
            assert "horse" not in out and "battery" not in out and "charlie" not in out, line

    def test_underscore_prefixed_password_label_caught(self):
        # (?<![A-Za-z]) anchor: DB_PASSWORD: <value> free-text form is redacted
        # even though \b would not match inside the underscore-joined label.
        out = s.scrub("DB_PASSWORD: hunter2secretvalue")
        assert R in out and "hunter2secretvalue" not in out

    def test_env_quoted_value_without_secret_hint_preserved(self):
        # The secret-key-name gate still applies: a quoted value under a benign
        # key (no KEY/SECRET/TOKEN/… in the name) must NOT be redacted.
        assert s.scrub("MESSAGE='hello world foo'") == "MESSAGE='hello world foo'"
