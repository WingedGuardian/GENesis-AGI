"""Tests for the incident-framing advisory in scripts/hooks/commit-msg.

WHY THIS LAYER EXISTS. Public artifacts must not assert, in past tense, that
this system had a security failure. The existing scheduled leak review does
catch that — but it is a MERGE gate: it stops the text reaching `main` and
cannot stop it reaching the internet, which happens at `git push`. And on this
repo the branch COMMIT message is what lands on `main` (squash merges take
`COMMIT_MESSAGES`), so the commit is the artifact that matters and the one that
cannot be amended after pushing — `git_push_guard` forbids force-pushing a
public branch, by design.

WHY ADVISORY, NOT BLOCKING. At commit time the fix is free (`--amend` before
push costs nothing), so a warning is actionable. Blocking would also stop
background sessions from committing, which nothing here justifies. The honest
argument is not cost-of-fix, though: you cannot justify blocking on a matcher
whose recall you have not measured. Recall IS measured below, so record the
escalation criterion rather than leaving it to taste — revisit blocking only at
>=90% recall on a labelled set with <=1 false positive over the same corpus.

WHY THE SUBJECT LINE ONLY. Measured, not inherited from what the regex happened
to do. `TestMeasuredRates` re-derives both numbers from this repository's own
history on every run.

WHY ONLY ONE AXIS. A second axis — describing what still evades a guard — was
measured against this repo's commit messages and REJECTED: it fired mostly
false, because "bypass the gate" and "defeated the guard" are core vocabulary in
a repo whose job is building gates. A checker that cries wolf on normal language
gets muted, taking the useful axis with it. That judgement stays with the LLM
leak review.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts" / "hooks" / "commit-msg"

# Incident framing, phrased the ways people actually phrase it. Derived by
# enumerating the axis BY HAND — tense, voice, noun, and state word — because a
# corpus replay can only prove that shapes already written behave, and is
# structurally blind to a phrasing nobody happened to type. The first version of
# this check scored a clean 0% false-positive rate while catching 6 of these 23.
INCIDENT_SUBJECTS = [
    "stop captured session output persisting credentials",
    "fix: no longer persists API keys in plaintext",
    "the pane tail was storing tokens verbatim",
    "credentials were written to disk in plaintext",
    "we leaked an API key in the exit log",
    "this exposed the OAuth token to anyone reading the log",
    "the setup token was logged to the scrollback",
    "secrets were being written unencrypted",
    "the session wrote credentials in cleartext",
    "tokens landed in the log unredacted",
    "the key was persisted in plaintext",
    "we exposed a bearer token in captured output",
    "the passphrase was stored in the clear",
    "an API key leaked into the observations store",
    "credentials had been exposed in the transcript",
    "the token was recoverable from the log file",
    "we were logging secrets to disk",
    "this used to write the private key to the log",
    "the guard was leaking credentials into memory",
    "the cookie was stored unencrypted",
    "we stored a password in cleartext",
    "the crash log contained the raw token",
    "secrets ended up in the captured tail verbatim",
]

# Deliberately adjacent: same vocabulary, no incident claim. Several are real
# subjects from this repository.
BENIGN_SUBJECTS = [
    "fix(hooks): redact captured session output before storing it",
    "feat: add token bucket rate limiter for the routing chain",
    "chore(deps): bump cryptography from 42.0.1 to 42.0.5",
    "fix: the persistent connection pool leaks file descriptors",
    "docs: explain how the credential store is structured",
    "refactor: extract the api key loader into its own module",
    "fix: password field was not being trimmed before validation",
    "feat(security): scan commit messages for private addresses",
    "fix: secret_scrub no longer matches ordinary hyphenated names",
    "test: add fixtures for the token refresh path",
    "fix: reattaching always bypasses the capacity gate",
    "fix: memory leak in the embedding cache",
    "chore: rotate the CI signing key",
    "fix: plaintext email bodies are parsed correctly now",
    "feat: store credentials in the reference store",
    "fix: the vulnerable dependency was updated to 3.2.1",
    "docs: note that detection is shape-based and not exhaustive",
    "fix: avoid logging the full request body",
]


def _run(msg: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Invoke the hook the way git does, with the fingerprint layer disabled."""
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text(msg)
    return subprocess.run(
        ["bash", str(HOOK), str(f)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "GENESIS_RELEASE_FINGERPRINTS": str(tmp_path / "none.txt"),
        },
    )


def _advises(msg: str, tmp_path: Path) -> bool:
    r = _run(msg, tmp_path)
    assert r.returncode == 0, f"advisory must never block a commit:\n{r.stdout}"
    return "incident framing" in (r.stdout + r.stderr).lower()


def _pattern() -> re.Pattern:
    """The hook's own pattern, assembled from the hook's own definitions.

    Read out of the script rather than duplicated here, so the measurements
    below can never drift from what actually ships.
    """
    src = HOOK.read_text()
    parts: dict[str, str] = {}
    for name in ("_FRAMING_NOUN", "_FRAMING_STATE", "_FRAMING_PAST", "_FRAMING_STOPV"):
        m = re.search(rf"^{name}='(.*)'$", src, re.M)
        assert m, f"{name} not found in the hook"
        parts[name] = m.group(1)
    m = re.search(r'^FRAMING_PATTERNS="(.*)"$', src, re.M)
    assert m, "FRAMING_PATTERNS not found in the hook"
    pat = m.group(1)
    for name, value in parts.items():
        pat = pat.replace("${" + name + "}", value)
    assert "${" not in pat, f"unexpanded variable in the pattern: {pat[:80]}"
    return re.compile(pat, re.I)


class TestKnownPositiveControl:
    """The text that actually shipped must be caught.

    A matcher that finds nothing is indistinguishable from one that looks at
    nothing; only replaying a known positive tells them apart.
    """

    def test_the_message_that_shipped_is_caught(self, tmp_path):
        assert _advises("stop captured session output persisting credentials", tmp_path)

    def test_the_corrected_replacement_is_not_caught(self, tmp_path):
        assert not _advises(
            "fix(hooks): redact captured session output before storing it", tmp_path
        )


class TestMeasuredRates:
    """Both directions, re-derived on every run.

    A rate measured on one side of a tradeoff cannot distinguish a good filter
    from a blind one, and the side left unmeasured is the side that is wrong.
    """

    def test_recall_on_the_labelled_incident_set(self, tmp_path):
        rx = _pattern()
        missed = [s for s in INCIDENT_SUBJECTS if not rx.search(s)]
        assert not missed, (
            f"recall {len(INCIDENT_SUBJECTS) - len(missed)}/"
            f"{len(INCIDENT_SUBJECTS)}; missed: {missed}"
        )

    def test_no_false_fires_on_the_labelled_benign_set(self, tmp_path):
        rx = _pattern()
        fired = [s for s in BENIGN_SUBJECTS if rx.search(s)]
        assert not fired, f"fired on benign subjects: {fired}"

    def test_fire_rate_on_this_repositorys_real_subjects(self):
        """The gate: the threshold was fixed BEFORE measuring.

        Subject lines only. Body coverage was measured at 41 fires in 1,756
        messages (2.33%) and every one read as ordinary engineering prose — a
        credential noun and a leak verb land near each other by chance in a
        long body. That rate gets a checker muted, so the scope is the subject.
        """
        proc = subprocess.run(
            ["git", "-C", str(REPO), "log", "--format=%s", "--no-merges"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            pytest.skip("git history unavailable")
        subjects = [s for s in proc.stdout.splitlines() if s.strip()]
        if len(subjects) < 200:
            pytest.skip(f"shallow history ({len(subjects)} subjects); rate not meaningful")
        rx = _pattern()
        hits = [s for s in subjects if rx.search(s)]
        rate = len(hits) / len(subjects)
        assert rate <= 0.001, (
            f"fire rate {len(hits)}/{len(subjects)} ({rate:.3%}) exceeds the 0.1% "
            f"bar set before measuring; hits: {hits[:10]}"
        )


class TestScopeIsTheSubjectLine:
    def test_a_body_line_is_not_scanned(self, tmp_path):
        """Stated scope, pinned. Body coverage costs 2.33% false fires on this
        repo's own history, which is the profile that gets a checker muted."""
        msg = (
            "fix(hooks): redact captured session output before storing it\n"
            "\n"
            "the pane tail was storing tokens verbatim\n"
        )
        assert not _advises(msg, tmp_path)

    def test_the_subject_is_scanned_even_with_a_body(self, tmp_path):
        msg = "the pane tail was storing tokens verbatim\n\nsome ordinary body\n"
        assert _advises(msg, tmp_path)


class TestIncidentFramingIsFlagged:
    @pytest.mark.parametrize("subject", INCIDENT_SUBJECTS)
    def test_each_labelled_incident_subject(self, subject, tmp_path):
        assert _advises(subject, tmp_path)


class TestOrdinaryWorkIsNotFlagged:
    @pytest.mark.parametrize("subject", BENIGN_SUBJECTS)
    def test_each_labelled_benign_subject(self, subject, tmp_path):
        assert not _advises(subject, tmp_path)

    def test_present_tense_feature_work_is_not_an_incident(self, tmp_path):
        """The distinction that keeps the noun set usable: a past or stopping
        claim, not merely a credential noun beside a verb."""
        assert not _advises("feat: store credentials in the reference store", tmp_path)

    def test_a_cve_dependency_bump_is_not_an_incident(self, tmp_path):
        """`(was|were) vulnerable` was dropped: no credential-noun requirement,
        no corpus support, and it fires on exactly this — the shape that gets a
        checker muted."""
        assert not _advises("fix: the vulnerable dependency was updated", tmp_path)


class TestAdvisoryNeverBlocks:
    def test_exit_zero_even_when_flagged(self, tmp_path):
        r = _run("credentials were written to disk in plaintext", tmp_path)
        assert r.returncode == 0

    def test_message_names_the_commit_is_what_lands_on_main(self, tmp_path):
        r = _run("credentials were written to disk in plaintext", tmp_path)
        assert "squash" in r.stdout.lower() and "main" in r.stdout.lower()


class TestAdvisoryDoesNotSitOnTheBlockRemedy:
    """Ordering, and it is not cosmetic.

    Printed BEFORE the blocking stanza, the advisory landed directly above the
    block's own "commit with --no-verify" remedy — which disarms every hook,
    including the privacy gates the block belongs to. An advisory must not read
    as instructions for getting past a block it has nothing to do with.
    """

    def test_advisory_follows_the_block_remedy(self, tmp_path):
        msg = "credentials were written to disk in plaintext at 10.1.2.3\n"
        r = _run(msg, tmp_path)
        out = r.stdout
        if "--no-verify" not in out:
            pytest.skip("blocking layer did not fire; ordering not exercised")
        assert "incident framing" in out.lower()
        assert out.lower().index("incident framing") > out.index("--no-verify"), (
            "the advisory printed above the block's --no-verify remedy:\n" + out
        )
