"""Unit tests for scripts/hooks/secret_scrub.py — the stdlib secret scrubber.

The scrubber protects hook-captured telemetry (session observations, audit
trails) from persisting secrets, so these lock in BOTH directions: real secret
shapes are redacted, and benign look-alikes are left intact (over-redaction in
telemetry is cheap, but redacting normal env vars / paths would gut the notes).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


# ── scrub: bare-value credential shapes (the captured-output case) ────────


class TestScrubRedactsBareProviderTokens:
    """Credential shapes that arrive with NO label.

    Captured terminal output is the motivating case: a token printed by a CLI
    lands in the capture as a naked value, so only its SHAPE can save it. The
    labeled/assignment patterns never see it. Values here are synthetic.
    """

    def test_anthropic_setup_token(self):
        # ``sk-`` keys carry interior hyphens; an [A-Za-z0-9]-only class stops
        # at the first one, far short of any length floor, and passes it through.
        assert R in s.scrub("sk-ant-oat01-" + "A" * 40)

    def test_openrouter_key(self):
        assert R in s.scrub("sk-or-v1-" + "b" * 48)

    def test_openai_project_key(self):
        assert R in s.scrub("sk-proj-" + "c" * 40)

    def test_groq_key(self):
        assert R in s.scrub("gsk_" + "d" * 40)

    def test_nvidia_nim_key(self):
        assert R in s.scrub("nvapi-" + "e" * 40)

    def test_tavily_key(self):
        assert R in s.scrub("tvly-" + "f" * 32)

    def test_github_fine_grained_pat(self):
        assert R in s.scrub("github_pat_" + "g" * 40)

    def test_jwt(self):
        assert R in s.scrub("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0." + "h" * 40)

    def test_bare_token_alone_on_its_own_line(self):
        # The captured-output shape: no label, no assignment, just the value
        # surrounded by prose the tool printed around it.
        out = s.scrub("Your token (valid for 1 year):\n\nsk-ant-oat01-" + "Z" * 40 + "\n")
        assert R in out
        assert "Z" * 40 not in out


class TestScrubRedactsStructuredCredentials:
    """Credentials identified by STRUCTURE rather than a vendor prefix."""

    def test_discord_webhook_url(self):
        out = s.scrub("https://discord.com/api/webhooks/123456789012345678/" + "A" * 60)
        assert R in out
        assert "A" * 60 not in out

    def test_discord_webhook_url_alt_host(self):
        out = s.scrub("https://discordapp.com/api/webhooks/987654321098765432/" + "B" * 60)
        assert R in out

    def test_telegram_bot_token_bare(self):
        out = s.scrub("1234567890:AA" + "C" * 33)
        assert R in out
        assert "C" * 33 not in out

    def test_telegram_bot_token_in_api_url(self):
        # The form Genesis itself constructs (guardian alert, backup.sh,
        # install.sh) and therefore the form most likely to reach a pane tail.
        # A left anchor that rejects any preceding word character kills this,
        # because the id is preceded by the literal "bot".
        url = "https://api.telegram.org/bot7891234567:AAG1a2b3c4d5" + "e" * 24 + "/sendMessage"
        out = s.scrub(url)
        assert R in out, "token unredacted in the vendor's own URL form"
        assert "AAG1a2b3c4d5" not in out

    def test_telegram_bot_token_at_vendor_documented_length(self):
        # Telegram's public Bot API example carries a 34-char secret half. A
        # floor derived from one locally-observed token has no margin and
        # silently passes anything shorter.
        doc_shape = "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
        assert len(doc_shape.split(":")[1]) == 34
        assert R in s.scrub(doc_shape)


class TestScrubKeepsHyphenatedLookalikes:
    """The widened classes must not eat ordinary hyphenated text."""

    def test_short_sk_fragment(self):
        assert R not in s.scrub("sk-1")

    def test_hyphenated_prose(self):
        text = "a well-documented self-healing check-and-repair reconciliation pass"
        assert R not in s.scrub(text)

    def test_branch_name(self):
        assert R not in s.scrub("fix/scrub-capture-hygiene-and-more-words-here")

    def test_timestamps_and_ports_are_not_telegram_tokens(self):
        # Discriminating negatives for the digits:secret shape — these carry the
        # colon-separated form and must still survive.
        for benign in (
            "elapsed 18:13",
            "2026-09-01T12:34:56Z",
            "port 8080:8080 mapped",
            "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        ):
            assert R not in s.scrub(benign), benign

    def test_uuid_is_not_a_provider_token(self):
        assert R not in s.scrub("bed2f4dd-4224-4fe3-94b2-e18c9f99e0e3")

    def test_git_sha_is_not_a_token(self):
        assert R not in s.scrub("0400c706186786ccc92e8a8d7904773e7ed5f8b1")

    def test_timestamp_pair_is_not_a_telegram_token(self):
        assert R not in s.scrub("elapsed 18:13")

    def test_plain_discord_url_without_token(self):
        assert R not in s.scrub("https://discord.com/channels/123/456")


# ── scrub: bounded work on hostile input ─────────────────────────────────


class TestScrubDoesNotBacktrackCatastrophically:
    """``scrub`` runs inside a PostToolUse hook and on captured terminal tails,
    so its input is arbitrary machine output — long unbroken alphanumeric runs
    (base64 blobs, minified bundles, hex dumps) are ordinary, not adversarial.

    Greedy unbounded quantifiers followed by a required literal backtrack
    quadratically on exactly that shape. The threshold is deliberately loose:
    the unfixed patterns take ~30s on this input, so a multi-second ceiling
    separates linear from quadratic without being sensitive to machine speed.
    """

    def test_long_alphanumeric_run_completes_promptly(self):
        import time

        blob = "eyJ" + "A" * 40000
        start = time.perf_counter()
        s.scrub(blob)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"scrub took {elapsed:.1f}s — quadratic backtracking"

    def test_long_value_still_redacted_after_bounding(self):
        # Guard the fix did not over-correct into a fail-open: bounding the KEY
        # must not stop a long VALUE from being redacted.
        assert R in s.scrub("API_SECRET=" + "y" * 9000)

    def test_long_url_password_still_redacted_after_bounding(self):
        assert R in s.scrub("https://user:" + "p" * 900 + "@host.example/path")


class TestBoundingNeverFailsOpen:
    """Bounding a repeat to stop quadratic backtracking must not stop a real
    secret being redacted — the failure mode is silent, so each bound needs a
    control on the side it does NOT protect.

    The first attempt bounded the env-assignment KEY at 64. That did not merely
    miss long keys: it slid the match FORWARD past the name's secret-ish prefix,
    so `_SECRET_KEY_HINT` no longer saw `SECRET_`/`TOKEN_` and redaction stopped
    entirely. And it bounded a URL span labelled "hostname" that is in fact the
    USERINFO, capping usernames for no perf benefit at all.
    """

    # A value with no vendor prefix and no label: only the arm under test can
    # match it, so a pass cannot be borrowed from another pattern.
    UNLABELLED = "9f8e7d6c5b4a39281706abcdef0123456789"

    def test_the_isolation_control_holds(self):
        assert R not in s.scrub(self.UNLABELLED), (
            "the bare value is caught by some other arm — the tests below would "
            "pass without proving anything about the arm under test"
        )

    def test_long_env_key_is_still_redacted(self):
        for n in (10, 64, 65, 120, 400):
            line = f"SECRET_{'A' * n}={self.UNLABELLED}"
            assert R in s.scrub(line), f"env key of {n + 7} chars silently unredacted"

    def test_long_url_userinfo_is_still_redacted(self):
        for n in (5, 253, 254, 600):
            url = f"https://{'u' * n}:{'p' * 40}@host.example/path"
            assert R in s.scrub(url), f"userinfo of {n} chars silently unredacted"

    def test_long_secret_value_is_still_redacted(self):
        # The side the original bounding DID protect — kept as a guard so a
        # future fix cannot trade one direction for the other.
        assert R in s.scrub("API_SECRET=" + "y" * 9000)


class TestStructuredCredentialsAreCaseInsensitive:
    """Hosts and schemes are case-insensitive per RFC 3986, and real logs carry
    both forms."""

    def test_discord_webhook_uppercase_host(self):
        assert R in s.scrub("https://DISCORD.COM/api/webhooks/123456789012345678/" + "A" * 60)

    def test_discord_webhook_uppercase_scheme(self):
        assert R in s.scrub("HTTPS://discord.com/api/webhooks/123456789012345678/" + "A" * 60)

    def test_discord_webhook_mixed_case_app_host(self):
        assert R in s.scrub("https://DiscordApp.com/api/webhooks/123456789012345678/" + "A" * 60)

    def test_lowercase_still_works(self):
        assert R in s.scrub("https://discord.com/api/webhooks/123456789012345678/" + "A" * 60)


class TestEnvAssignmentAnchorKeepsBaselineRecall:
    """The anchor that removes the quadratic behaviour must not cost recall the
    shipping version already had."""

    V = "9f8e7d6c5b4a39281706abcdef0123456789"

    def test_underscore_prefixed_secret_name_still_redacted(self):
        assert R in s.scrub(f"_MY_SECRET={self.V}")

    def test_plain_secret_name_still_redacted(self):
        assert R in s.scrub(f"SECRET_KEY={self.V}")

    def test_empty_quoted_value_is_not_a_secret(self):
        # The dominant historical false positive: an ordinary code constant
        # whose NAME contains a secret word and whose value is empty.
        assert R not in s.scrub('_OAUTH_SRC=""')

    def test_trivial_quoted_value_is_not_a_secret(self):
        assert R not in s.scrub('API_MODE="on"')


class TestScrubIsLinearAcrossInputShapes:
    """A perf claim measured on one input shape is not a perf claim.

    The first regression here passed its benchmark because the benchmark was
    drawn from the exact character class the anchor excluded — the test
    validated the fix's own assumption. This matrix spans the classes that
    drive the env-assignment arm differently, so a pattern edit cannot go
    quadratic on one shape while the suite stays green on another.

    The 5s ceiling is deliberately loose (machine-speed tolerant): the broken
    shapes measured 13-16 SECONDS, healthy ones under 100ms.
    """

    SHAPES = {
        "all_alnum": "eyJ" + "A" * 40000,
        "underscore_pairs": "A_" * 20000,
        "screaming_snake": "_".join("ABCDEFGH" for _ in range(5000)),
        "sparse_underscores": ("A" * 199 + "_") * 200,
        "quoted_wall": '"' + "x" * 40000,
        "url_wall": "https://u:" + "p" * 40000,
        # PEM armour is the one arm that must reason ACROSS lines, so it is the
        # one that can go quadratic on repeated markers. Both directions of the
        # half-visible case belong in the matrix too: each drives the adjacent
        # key-material walk rather than the matched-pair path.
        "repeated_pem_headers": "-----BEGIN RSA PRIVATE KEY-----\n" * 8192,
        "repeated_pem_footers": "-----END RSA PRIVATE KEY-----\n" * 8192,
        "pem_header_then_body": (
            "-----BEGIN RSA PRIVATE KEY-----\n" + ("QUFB" * 16 + "\n") * 4000
        ),
        # Decoration recognition inspects the TAIL of every line, so the two
        # shapes that exercise it belong here: a wall of decorated body lines,
        # and prose whose lines end in a long token (the near-miss that must
        # stay cheap as well as stay OUT of the key-material class).
        "decorated_body_wall": ("2026-09-02 12:00:00 " + "QUFB" * 16 + "\n") * 4000,
        "long_tail_prose": (
            "crash at 0400c706186786ccc92e8a8d7904773e7ed5f8b1abc\n" * 6000
        ),
    }

    @pytest.mark.parametrize("shape", sorted(SHAPES))
    def test_shape_completes_promptly(self, shape):
        import time

        start = time.perf_counter()
        s.scrub(self.SHAPES[shape])
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"{shape}: {elapsed:.1f}s — quadratic behaviour"


class TestEnvKeyCeilingResidual:
    """The {2,512}+ ceiling has ONE residual, accepted deliberately and pinned
    here so it is a stated behaviour, not a surprise.

    The lookbehind admits a match start after `_` (that is what keeps
    `2FA_TOKEN=` catchable), so a key LONGER than 512 chars with interior
    underscores can match a late suffix that no longer contains the secret-ish
    hint — and the value is then not redacted by THIS arm. Exposure is zero
    measured (the longest real key on this install is 31 chars); this test
    firing in anger is the trigger to revisit the ceiling.
    """

    V = "9f8e7d6c5b4a39281706abcdef0123456789"

    def test_keys_up_to_the_ceiling_redact(self):
        for n in (31, 120, 400, 505):
            assert R in s.scrub(f"SECRET_{'A' * n}={self.V}"), f"key len {n + 7}"

    def test_the_documented_residual_at_600_chars(self):
        # No interior underscore after SECRET_ -> lookbehind blocks every
        # restart, so an over-ceiling key is a CLEAN MISS (better than the old
        # fail-open, which broke redaction at 65 chars).
        out = s.scrub(f"SECRET{'A' * 600}={self.V}")
        assert R not in out  # documented: clean miss past the ceiling


class TestDiffLineTokensAreRedacted:
    """A token on a git-diff removed line arrives as `-<token>` with no
    separator. The version on main redacts these; an anchor that excludes `-`
    from the start position regressed them to raw. Pane tails routinely hold
    `git diff` / `git log -p` output, so this is an ordinary shape."""

    @pytest.mark.parametrize(
        "line",
        [
            "-ghp_" + "a" * 36,
            "-AKIAIOSFODNN7EXAMPLE",
            "-AIza" + "c" * 35,
            "-sk-ant-oat01-" + "T" * 40,
            "+ghp_" + "a" * 36,  # added lines too
        ],
    )
    def test_diff_prefixed_token_redacts(self, line):
        assert R in s.scrub(line), line[:30]

    def test_hyphenated_prose_still_survives(self):
        # The negatives that justified the old exclusion must keep passing.
        assert R not in s.scrub(
            "a well-documented self-healing check-and-repair reconciliation pass"
        )


class TestPemPrivateKeysAreRedacted:
    """`cat ~/.ssh/id_ed25519` in the last 200 lines of a pane is an ordinary
    accident and the highest-value secret on the box. No shape pattern can
    catch a base64 body; the BEGIN/END armour can."""

    def _pem(self, kind="OPENSSH"):
        body = "\n".join("QUFB" * 16 for _ in range(6))
        # kind="" is the real PKCS#8 form `BEGIN PRIVATE KEY` — single space,
        # not the double space naive interpolation produces.
        label = f"{kind} PRIVATE KEY" if kind else "PRIVATE KEY"
        return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"

    @pytest.mark.parametrize("kind", ["OPENSSH", "RSA", "EC", ""])
    def test_pem_block_is_redacted(self, kind):
        text = f"here is the file:\n{self._pem(kind)}\nafter"
        out = s.scrub(text)
        assert "QUFB" not in out, f"{kind or 'generic'} PEM body survived"
        assert "here is the file" in out and "after" in out

    def test_unterminated_header_redacts_the_key_run(self):
        """A HALF-VISIBLE key must still be redacted.

        This assertion used to read `"QUFB" in out` — it pinned a live leak.
        The capture window (`-S -200`) and the 256KB input cap both cut at
        arbitrary positions, so a key whose END marker was clipped away reached
        the log with its body intact. Requiring BOTH delimiters means the
        clipped case is exactly the case that leaks.
        """
        import time

        blob = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(
            "QUFB" * 16 for _ in range(400)
        )
        start = time.perf_counter()
        out = s.scrub(blob)
        assert time.perf_counter() - start < 5.0
        assert "QUFB" not in out, "clipped-END key body survived"

    def test_orphan_end_marker_redacts_the_preceding_key_run(self):
        """The mirror case: the BEGIN scrolled off the top of the window."""
        body = "\n".join("QUFB" * 16 for _ in range(6))
        text = f"ordinary diagnostic line\n{body}\n-----END RSA PRIVATE KEY-----\nafter"
        out = s.scrub(text)
        assert "QUFB" not in out, "clipped-BEGIN key body survived"
        assert "ordinary diagnostic line" in out and "after" in out

    def test_a_lone_marker_in_prose_does_not_blank_the_diagnostic(self):
        """Redaction walks the ADJACENT key-material run and stops at the first
        ordinary line, so one stray marker (grep output, a docstring) cannot
        take the surrounding diagnostic with it."""
        text = (
            "line one of the crash\n"
            "grep: id_rsa: -----BEGIN RSA PRIVATE KEY-----\n"
            "line three of the crash\n"
            "line four of the crash\n"
        )
        out = s.scrub(text)
        for keep in ("line one of the crash", "line three", "line four"):
            assert keep in out, f"{keep!r} was blanked by a lone marker:\n{out}"

    def test_repeated_unmatched_headers_stay_linear(self):
        """The measured quadratic: the old both-delimiter regex retried its
        cross-line body scan at EVERY header, 25.5s on a 256KB input of
        repeated headers (ratio 4.0 at 2x input) — past the exit-capture
        timeout, so the whole diagnostic was discarded."""
        import time

        hdr = "-----BEGIN RSA PRIVATE KEY-----\n"
        blob = hdr * (262144 // len(hdr))
        start = time.perf_counter()
        s.scrub(blob)
        assert time.perf_counter() - start < 5.0

    def test_mismatched_armour_labels_still_redact(self):
        body = "\n".join("QUFB" * 16 for _ in range(4))
        text = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
        assert "QUFB" not in s.scrub(text)

    def test_crlf_line_endings_are_handled(self):
        body = "\r\n".join("QUFB" * 16 for _ in range(4))
        text = f"-----BEGIN RSA PRIVATE KEY-----\r\n{body}\r\n-----END RSA PRIVATE KEY-----"
        assert "QUFB" not in s.scrub(text)

    def test_legacy_encrypted_armour_puts_headers_before_the_body(self):
        """RFC 1421 / OpenSSL legacy-encrypted armour — what
        `openssl genrsa -aes128 -traditional` and `ssh-keygen -m PEM -N` emit —
        carries `Proc-Type:`/`DEK-Info:` and a BLANK LINE between the marker and
        the base64. A walk that requires the body on the very next line stops at
        the metadata and writes the whole key out.

        VERIFIED against a real `openssl genrsa -aes128` artifact before this
        was handled: 14 of 14 visible body lines reached the log. Synthetic
        fixtures with no header lines cannot see this, which is why it survived
        a green suite.
        """
        body = "\n".join("QUFB" * 16 for _ in range(6))
        text = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "Proc-Type: 4,ENCRYPTED\n"
            "DEK-Info: AES-128-CBC,B6F35A9DA8465EEE6BD4B061D097D3C5\n"
            "\n"
            f"{body}\n"
            "trailing diagnostic line\n"
        )  # END deliberately absent: the capture window cut it off
        out = s.scrub(text)
        assert "QUFB" not in out, f"encrypted-armour body survived:\n{out}"
        assert "trailing diagnostic line" in out

    def test_encrypted_pkcs8_armour_is_redacted(self):
        body = "\n".join("QUFB" * 16 for _ in range(4))
        text = f"-----BEGIN ENCRYPTED PRIVATE KEY-----\n{body}\n-----END ENCRYPTED PRIVATE KEY-----"
        assert "QUFB" not in s.scrub(text)

    def test_pgp_secret_key_block_is_redacted(self):
        """`gpg --export-secret-keys -a` armour says PRIVATE KEY BLOCK."""
        body = "\n".join("lQVYBGhA" * 8 for _ in range(4))
        text = f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n{body}\n-----END PGP PRIVATE KEY BLOCK-----"
        assert "lQVYBGhA" not in s.scrub(text)

    def test_the_short_final_body_line_is_redacted_too(self):
        """A base64 body's LAST line is short by construction (a 2048-bit RSA
        key ends in a 2-char line), so a minimum-length run bound would leave
        the key's tail sitting in the log."""
        body = "\n".join("QUFB" * 16 for _ in range(3))
        text = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\nQUFBQUFB\nordinary line\n"
        out = s.scrub(text)
        assert "QUFBQUFB" not in out, f"key tail survived:\n{out}"
        assert "ordinary line" in out

    def test_two_unrelated_markers_do_not_swallow_the_span(self):
        """The classic shape: one `grep` hit names a BEGIN, another names an
        END, and everything between them is ordinary diagnostic output. Pairing
        them destroys the crash context the log exists for.

        VERIFIED before the interior check existed: 7 diagnostic lines in, 1
        out.
        """
        text = "\n".join(
            ["tests/a.py:12:  key = '-----BEGIN RSA PRIVATE KEY-----'"]
            + [f"crash line {i} KEEPME" for i in range(1, 7)]
            + ["tests/b.py:99:  end = '-----END RSA PRIVATE KEY-----'"]
            + ["crash line 7 KEEPME"]
        )
        out = s.scrub(text)
        kept = sum(1 for ln in out.splitlines() if "KEEPME" in ln)
        assert kept == 7, f"only {kept}/7 diagnostic lines survived:\n{out}"

    def test_a_real_block_is_still_redacted_whole(self):
        """Guard for the check above: the interior test must not stop a genuine
        block — whose interior IS key material — from being redacted."""
        body = "\n".join("QUFB" * 16 for _ in range(20))
        text = f"before\n-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\nafter"
        out = s.scrub(text)
        assert "QUFB" not in out
        assert "before" in out and "after" in out

    # ── captured output is frequently DECORATED ────────────────────────────
    # A column-0 test for "is this line key material" is an assumption about
    # the CAPTURE, not about the key. Enumerated as a class rather than as the
    # one shape a reviewer happened to name: MEASURED across 7 decorations x 3
    # clipping states, 4 decorations wrote the whole body out before this was
    # handled, while plain / indented / diff-`+` did not — and those three pass
    # only because `+` is a base64 character and `.strip()` covers indentation.
    # Passing by accident of the alphabet is not coverage.
    _DECORATIONS = {
        "log_timestamp": lambda ln: f"2026-09-02 12:00:00 {ln}",
        "journalctl": lambda ln: f"host genesis[123]: {ln}",
        "diff_removed": lambda ln: f"-{ln}",
        "diff_added": lambda ln: f"+{ln}",
        "indented": lambda ln: f"    {ln}",
        "pipe_gutter": lambda ln: f"  | {ln}",
    }

    def _armoured(self, kind="RSA"):
        """Real RFC 1421 shape: metadata headers, a blank line, then body."""
        body = [("QUFB" * 16) for _ in range(6)]
        return (
            [f"-----BEGIN {kind} PRIVATE KEY-----",
             "Proc-Type: 4,ENCRYPTED",
             "DEK-Info: AES-128-CBC,B6F35A9DA8465EEE6BD4B061D097D3C5",
             ""]
            + body
            + [f"-----END {kind} PRIVATE KEY-----"]
        )

    @pytest.mark.parametrize("decoration", sorted(_DECORATIONS))
    @pytest.mark.parametrize("clip", ["paired", "end_clipped", "begin_clipped"])
    def test_decorated_key_material_is_redacted(self, decoration, clip):
        lines = self._armoured()
        if clip == "end_clipped":
            lines = lines[:-1]
        elif clip == "begin_clipped":
            lines = lines[1:]
        decorate = self._DECORATIONS[decoration]
        out = s.scrub("\n".join(decorate(ln) for ln in lines))
        assert "QUFB" not in out, f"{decoration}/{clip} body survived:\n{out}"

    def test_decoration_handling_does_not_reopen_the_marker_swallow(self):
        """Guard for the guard: recognising decorated bodies must not make
        ordinary prose look like key material and re-open the two-marker
        swallow. Prose ends in a word, not in a long base64 run."""
        text = "\n".join(
            ["2026-09-02 12:00:00 grep: -----BEGIN RSA PRIVATE KEY-----"]
            + [f"2026-09-02 12:00:0{i} crash line {i} KEEPME" for i in range(1, 7)]
            + ["2026-09-02 12:00:09 grep: -----END RSA PRIVATE KEY-----"]
        )
        out = s.scrub(text)
        kept = sum(1 for ln in out.splitlines() if "KEEPME" in ln)
        assert kept == 6, f"only {kept}/6 decorated diagnostic lines survived:\n{out}"

    def test_armour_metadata_is_not_mistaken_for_body(self):
        """`DEK-Info:` ends in a long hex run that reads as base64, so a
        tail-only test would stop the body search on the metadata line and walk
        straight past the key."""
        assert not s._body_line(
            "DEK-Info: AES-128-CBC,B6F35A9DA8465EEE6BD4B061D097D3C5"
        )
        assert s._body_line("2026-09-02 12:00:00 " + "QUFB" * 16)

    def test_a_certificate_is_not_a_private_key(self):
        """Only PRIVATE KEY armour redacts. A certificate is public material,
        and treating it as a key would blank ordinary TLS diagnostics."""
        body = "\n".join("Q0VSVA" * 8 for _ in range(4))
        text = f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----"
        out = s.scrub(text)
        assert "Q0VSVA" in out, "a public certificate was redacted as a key"


class TestAdditionalVendorPrefixes:
    @pytest.mark.parametrize(
        "tok",
        [
            "sk_live_" + "a" * 24,          # Stripe secret
            "rk_live_" + "b" * 24,          # Stripe restricted
            "ASIA" + "QRSTUVWXYZ12345A",    # AWS STS temporary
            "npm_" + "c" * 36,
            "pypi-" + "d" * 60,
            "csk-" + "e" * 48,  # Cerebras — a miss the left-anchor exposed
        ],
    )
    def test_prefix_redacts(self, tok):
        assert R in s.scrub("value: " + tok), tok[:16]

    def test_asia_the_word_is_not_a_credential(self):
        assert R not in s.scrub("shipping to ASIA next week")

    def test_stripe_lookalike_prose_survives(self):
        assert R not in s.scrub("the sk_live_migration plan doc")


class TestPatternsStayPortable:
    """The scrubber runs under whatever bare `python3` the install has —
    `scripts/cc_exit_capture.sh` invokes it by name, deliberately NOT the venv
    interpreter, so a broken venv cannot break the dying-pane capture.

    Possessive quantifiers (`*+`, `{m,n}+`) are Python 3.11+ ONLY (bpo-433030).
    An install whose system `python3` is older — Ubuntu 22.04 ships 3.10, and
    `scripts/install.sh` installs `python3.12` as a SEPARATE binary rather than
    replacing it — would raise `re.error` at import, and every captured tail
    would be withheld forever. Plain bounded repeats give the same linearity
    (MEASURED: whole-blob scrubbing stays linear, 39KB/78KB/156KB =
    349/705/1441ms) and the same matches (MEASURED: 0 differing outputs over
    20,270 strings).
    """

    def test_no_possessive_quantifiers_in_any_compiled_pattern(self):
        """Checks the COMPILED patterns, not the file text — prose may name the
        construct (this module's own comments do); only a pattern can break the
        import."""
        import re as _re

        possessive = _re.compile(r"(?:\*\+|\?\+|\{\d+(?:,\d*)?\}\+)")
        offenders = {
            name: possessive.findall(obj.pattern)
            for name, obj in vars(s).items()
            if isinstance(obj, _re.Pattern) and possessive.search(obj.pattern)
        }
        assert not offenders, (
            f"possessive quantifier(s) in {offenders} require Python 3.11+; "
            "the hook must import under the install's bare python3"
        )

    def test_the_portability_guard_can_actually_fail(self):
        """Guard-the-guard: the check above reads `.pattern` off module-level
        compiled regexes, so it is worth proving it SEES one."""
        import re as _re

        possessive = _re.compile(r"(?:\*\+|\?\+|\{\d+(?:,\d*)?\}\+)")
        assert possessive.search(r"(?P<key>_*+[A-Z]{2,512}+)")
        assert not possessive.search(r"(?P<key>_{0,4}[A-Z]{2,512})")
