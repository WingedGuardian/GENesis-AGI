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
    # Reviewer-cited (PR #1623). Each fired before the atom groups were bounded
    # and the progressive forms were given a tense requirement; each is ordinary
    # forward-looking work, and two of them (`unrecoverable`, `restored`) carried
    # the OPPOSITE meaning to the thing being detected.
    "fix: restored credential loading after update",
    "docs: explain prerecorded token fixtures",
    "fix: ensure deleted tokens are unrecoverable",
    "feat: support storing API keys in the system keyring",
    "feat: logging tokens to metrics",
    "fix: keyboard logging",
    # The stopping phrase was an inline literal and so escaped the boundary
    # sweep — `stop` inside `stopgap`/`backstop`/`nonstop`/`stopwatch`,
    # `prevent` inside `preventive`, `used to` inside `reused to`.
    "feat: add a stopgap that writes the api key cache",
    "chore: backstop the retry loop that logs the token id",
    "fix: nonstop retries stores the session key",
    "feat: stopwatch timer logs the api key latency",
    "docs: preventive notes on how we store keys",
    "fix: preventative cleanup stores the cookie jar",
    "feat: reused to log the refresh token path",
]

# Subjects that DO fire and arguably should not. Kept as a named, asserted set
# rather than as prose, so that closing one is visible instead of silent. Each
# is a face of the same limit: one spelling, two grammars, and only a parser
# separates them — see the KNOWN LIMIT notes in the hook.
KNOWN_RESIDUE_SUBJECTS = [
    # Present passive participle: forward-looking, reads as past to the pattern.
    "feat: API keys are now stored in the system keyring",
    # Part of speech: `logs` is a noun here and a verb in "no longer logs the
    # token", which this branch exists to catch.
    "docs: prevent flaky logs from the token refresh test",
    "chore: stop the noisy logs in the key rotation job",
    "fix: prevents duplicate logs for the token refresh path",
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
            # A real `git commit` runs the hook under the user's UTF-8 locale.
            # With no LC_ALL the subprocess inherits C, where `grep` counts the
            # `[^.]{0,45}` windows in BYTES rather than characters — so the tests
            # would exercise a different engine configuration than production,
            # and a non-ASCII subject could straddle the window in one and not
            # the other. Pin it rather than inherit whatever the runner has.
            "LC_ALL": "C.UTF-8",
            "HOME": str(tmp_path),
            "GENESIS_RELEASE_FINGERPRINTS": str(tmp_path / "none.txt"),
        },
    )


def _advises(msg: str, tmp_path: Path) -> bool:
    r = _run(msg, tmp_path)
    assert r.returncode == 0, f"advisory must never block a commit:\n{r.stdout}"
    return "incident framing" in (r.stdout + r.stderr).lower()


def _atom_groups() -> dict[str, str]:
    """Every `_FRAMING_*` group the hook defines, DISCOVERED not enumerated.

    A hard-coded name list silently ignores a group added later — which is
    exactly how an unbounded group would slip past the boundary invariant
    below. Discovery makes a new group opt OUT loudly instead of in silently.
    """
    groups = {
        m.group(1): m.group(2)
        for m in re.finditer(r"^(_FRAMING_[A-Z0-9_]+)='(.*)'$", HOOK.read_text(), re.M)
    }
    assert groups, "no _FRAMING_* atom groups found in the hook"
    return groups


def _pattern() -> re.Pattern:
    """The hook's own pattern, assembled from the hook's own definitions.

    Read out of the script rather than duplicated here, so the measurements
    below can never drift from what actually ships.

    ONE SUBSTITUTION, named because it is the gap in this instrument: the hook
    runs `grep -qiE`, and this compiles the same source with Python `re`. The
    labelled sets do not rely on it — `TestIncidentFramingIsFlagged` and
    `TestOrdinaryWorkIsNotFlagged` go through `_advises()`, which invokes the
    real hook — so only the corpus rate rests on the substitution.
    """
    src = HOOK.read_text()
    parts = _atom_groups()
    m = re.search(r'^FRAMING_PATTERNS="(.*)"$', src, re.M)
    assert m, "FRAMING_PATTERNS not found in the hook"
    pat = m.group(1)
    # Groups may reference one another; expand to a fixed point.
    for _ in range(len(parts) + 1):
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
        # Split on the LF RECORD DELIMITER only. `str.splitlines()` also breaks
        # on U+0085, U+2028 and U+2029, which a commit subject may legitimately
        # contain — the shell hook's `head`/`grep` pipeline keeps them INSIDE the
        # subject, so a subject that really does fire would be cut into two
        # non-matching strings here and the measured rate would UNDERCOUNT the
        # hook's real behaviour. A rate that measures something other than what
        # ships is worse than no rate (Codex P2, PR #1623).
        subjects = [s for s in proc.stdout.split("\n") if s.strip()]
        if len(subjects) < 200:
            pytest.skip(f"shallow history ({len(subjects)} subjects); rate not meaningful")
        rx = _pattern()
        hits = [s for s in subjects if rx.search(s)]
        rate = len(hits) / len(subjects)
        # Asserted on the COUNT, with the rate and its denominator still reported
        # so the 0.1% bar stays visible and re-derived. The bar over this corpus
        # tolerates exactly one hit and fails at two — and what it counts is
        # something this layer cannot prevent, because layer 3 never blocks a
        # commit. Two genuine TRUE positives entering history would therefore
        # redden the suite on an unrelated future PR, with the failure text
        # blaming a pattern that had done its job. The tolerance is the same
        # number either way; the message is what changes.
        # Derived from the bar rather than frozen at today's n: 1737 x 0.001 is
        # 1.7, so this is exactly `rate <= 0.001` now, and stays faithful to the
        # declared bar as history grows instead of silently tightening.
        allowed = max(1, int(0.001 * len(subjects)))
        assert len(hits) <= allowed, (
            f"{len(hits)} of {len(subjects)} subjects fire ({rate:.3%}; bar 0.1% "
            f"set before measuring). One TRUE positive in history is expected and "
            f"tolerated — read the hits before touching the pattern, because two "
            f"means either the pattern broadened or two real incident-framed "
            f"subjects landed: {hits[:10]}"
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


class TestCredentialNounsAreWordsNotSubstrings:
    """Two alternatives are short enough to live inside ordinary words.

    `cred` sits in `sacred`, `key` in `monkey`, `turnkey`, `keyboard`. Unbounded,
    they fire on neutral subjects — and this is the ADVISORY axis, where a false
    fire teaches whoever hits it to ignore the next one, taking the true
    positives with it. That is the same reasoning that scoped the check to the
    subject line and rejected the second axis; it applies inside the pattern too.
    """

    @pytest.mark.parametrize(
        "subject",
        [
            "fix: logged monkey patch state for the retry loop",
            "refactor: store the sacred ordering the parser relies on",
            "feat: log turnkey provisioning progress to the dashboard",
            "chore: stop writing keyboard shortcuts into the cache",
            "docs: record the sacred invariants nobody may change",
        ],
    )
    def test_a_credential_noun_inside_a_word_does_not_fire(self, subject, tmp_path):
        assert not _advises(subject + "\n", tmp_path), subject

    @pytest.mark.parametrize(
        "subject",
        [
            "the key was persisted in plaintext",
            "we logged the creds during startup",
            "keys were written to the log unredacted",
        ],
    )
    def test_the_bare_nouns_STILL_fire_as_whole_words(self, subject, tmp_path):
        """CONTROL, and load-bearing: deleting `cred`/`key` from the alternation
        would also pass every test above while dropping real coverage. The
        boundary is the fix, not the removal."""
        assert _advises(subject + "\n", tmp_path), subject


class TestTheAdvisoryNeverEchoesABlockedFingerprint:
    """Layer 2 reports LINE NUMBERS ONLY because the matched text IS the private
    value. The advisory printed the whole subject underneath it — handing back,
    into commit output and the session transcript, exactly what the block above
    had just refused to name.
    """

    @staticmethod
    def _run_with_fingerprint(subject: str, tmp_path: Path):
        fp = tmp_path / "fingerprints.txt"
        fp.write_text("# an install fingerprint\nULTRA_PRIVATE_TOKEN_123\n")
        f = tmp_path / "COMMIT_EDITMSG"
        f.write_text(subject + "\n")
        return subprocess.run(
            ["bash", str(HOOK), str(f)],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            # A real `git commit` runs the hook under the user's UTF-8 locale.
            # With no LC_ALL the subprocess inherits C, where `grep` counts the
            # `[^.]{0,45}` windows in BYTES rather than characters — so the tests
            # would exercise a different engine configuration than production,
            # and a non-ASCII subject could straddle the window in one and not
            # the other. Pin it rather than inherit whatever the runner has.
            "LC_ALL": "C.UTF-8",
                "HOME": str(tmp_path),
                "GENESIS_RELEASE_FINGERPRINTS": str(fp),
            },
        )

    def test_a_blocked_fingerprint_is_not_echoed_by_the_advisory(self, tmp_path):
        subject = "fix: API key ULTRA_PRIVATE_TOKEN_123 was stored in plaintext"
        r = self._run_with_fingerprint(subject, tmp_path)
        out = r.stdout + r.stderr
        assert "install-specific private identifier" in out, "layer 2 must have blocked"
        assert "incident framing" in out.lower(), "the framing advisory must still fire"
        assert "ULTRA_PRIVATE_TOKEN_123" not in out, (
            "the advisory echoed the private value the block above withheld"
        )
        assert "subject withheld" in out

    def test_a_fingerprint_in_the_BODY_does_not_withhold_a_clean_subject(self, tmp_path):
        """The suppression is scoped to the SUBJECT, not to the whole message.

        Layer 2 blocks on a match anywhere, which is right. But withholding the
        advisory's subject because a BODY line matched suppresses a subject that
        was clean and asserts something untrue about it — over-broad in the safe
        direction, and still a cost paid where nothing was concealed.
        """
        fp = tmp_path / "fingerprints.txt"
        fp.write_text("# an install fingerprint\nULTRA_PRIVATE_TOKEN_123\n")
        f = tmp_path / "COMMIT_EDITMSG"
        subject = "the key was persisted in plaintext"
        f.write_text(subject + "\n\nseen while testing ULTRA_PRIVATE_TOKEN_123 here\n")
        r = subprocess.run(
            ["bash", str(HOOK), str(f)],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "LC_ALL": "C.UTF-8",
                "HOME": str(tmp_path),
                "GENESIS_RELEASE_FINGERPRINTS": str(fp),
            },
        )
        out = r.stdout + r.stderr
        assert "install-specific private identifier" in out, "layer 2 must still block"
        assert "ULTRA_PRIVATE_TOKEN_123" not in out, "the body value must not be echoed"
        assert "incident framing" in out.lower()
        assert subject in out, "a clean subject must still be quoted"
        assert "subject withheld" not in out

    def test_an_ordinary_framing_subject_is_STILL_quoted(self, tmp_path):
        """CONTROL. Withholding always would make the advisory useless — the
        author needs to see which line is being flagged. Only the fingerprint
        case is suppressed, and only because that layer's whole contract is not
        naming what it matched.
        """
        subject = "the key was persisted in plaintext"
        r = self._run_with_fingerprint(subject, tmp_path)
        out = r.stdout + r.stderr
        assert "install-specific private identifier" not in out
        assert subject in out, "a non-fingerprint subject must still be quoted"


class TestEveryAtomGroupIsWordBounded:
    """The boundary invariant, stated once instead of per offender.

    Bounding only the alternatives a reviewer happened to name leaves the same
    class alive in every other group — which is what happened: `cred`/`key` were
    bounded, and the next round found `stored` inside `restored`, `recorded`
    inside `prerecorded`, and `recoverable` inside `unrecoverable`. That last one
    inverts the meaning outright, firing on the subject that says the tokens are
    gone. So the rule is checked STRUCTURALLY, over groups discovered from the
    hook, and a group added unbounded later fails here rather than in review.
    """

    def test_the_composed_pattern_contains_no_inline_group(self):
        """The hole that let the invariant be claimed while it was false.

        Checking only the discovered `_FRAMING_*` variables cannot see a group
        written inline in `FRAMING_PATTERNS` — and that is exactly where the one
        unbounded group was hiding, so the sweep that bounded "every group"
        missed the loosest branch in the pattern. Every group must therefore be
        a hoisted variable, which is what brings it under the check below.
        """
        raw = re.search(
            r'^FRAMING_PATTERNS="(.*)"$', HOOK.read_text(), re.M
        ).group(1)
        residue = re.sub(r"\$\{_FRAMING_[A-Z0-9_]+\}|\[\^\.\]\{\d+,\d+\}", "", raw)
        assert residue.strip("|") == "", (
            "FRAMING_PATTERNS contains an inline group, which no boundary check "
            "can reach — hoist it into a _FRAMING_* variable: "
            f"{residue.strip('|')!r}"
        )

    def test_every_atom_in_every_group_is_bounded(self):
        """Checked per ATOM, not by the group's first and last characters.

        A group can open `\\b(` and close `)\\b` and still be unbounded in the
        middle — `\\b(dumped)\\b[^.]{0,30}(cred|key)\\b` passes a first/last-character
        check and still matches `sacred`, which is the very bug this class is
        named after.
        """
        offenders: dict[str, list[str]] = {}
        for name, value in _atom_groups().items():
            # Split into the alternation groups the value is built from, then
            # require each to be immediately preceded by \b and followed by \b
            # (optionally through the shared `s?` plural suffix).
            for m in re.finditer(r"(\\b)?\(([^)]*)\)(s\?)?(\\b)?", value):
                lead, body, _plural, trail = m.groups()
                if not lead or not trail:
                    offenders.setdefault(name, []).append(body)
        assert not offenders, (
            "every alternation group must sit between \\b bounds — an unbounded "
            f"atom matches inside unrelated words: {offenders}"
        )

    @pytest.mark.parametrize(
        "subject",
        [
            "fix: restored credential loading after update",
            "docs: explain prerecorded token fixtures",
            "fix: ensure deleted tokens are unrecoverable",
        ],
    )
    def test_a_state_or_verb_atom_inside_a_word_does_not_fire(self, subject, tmp_path):
        assert not _advises(subject + "\n", tmp_path), subject

    @pytest.mark.parametrize(
        "subject",
        [
            "the token was stored in plaintext",
            "the crash log contained the raw token",
            "the key was recoverable from the log file",
        ],
    )
    def test_the_bare_atoms_STILL_fire_as_whole_words(self, subject, tmp_path):
        """CONTROL. Deleting these atoms would also pass the cases above while
        dropping real coverage — the boundary is the fix, not the removal."""
        assert _advises(subject + "\n", tmp_path), subject


class TestProgressiveVerbsNeedPastTenseContext:
    """A bare gerund is not a past-tense claim.

    `storing` sits equally in "was storing tokens" (an incident) and "support
    storing API keys" (a feature), so on its own it is no evidence at all. The
    progressive forms therefore require a past auxiliary; the imperative case
    ("stop ... persisting credentials") is the stopping-phrase branch, which is
    unaffected. Recall over the labelled incident set is unchanged at 23/23.
    """

    @pytest.mark.parametrize(
        "subject",
        [
            "feat: support storing API keys in the system keyring",
            "feat: logging tokens to metrics",
            "chore: add a helper for writing credentials to the keyring",
        ],
    )
    def test_a_bare_gerund_is_not_an_incident(self, subject, tmp_path):
        assert not _advises(subject + "\n", tmp_path), subject

    @pytest.mark.parametrize(
        "subject",
        [
            "we were logging secrets to disk",
            "the guard was leaking credentials into memory",
            "the pane tail was storing tokens",
        ],
    )
    def test_a_gerund_WITH_a_past_auxiliary_still_fires(self, subject, tmp_path):
        """CONTROL, and load-bearing: dropping the progressive forms entirely
        would also pass the cases above while blinding the detector to the
        commonest incident phrasing there is."""
        assert _advises(subject + "\n", tmp_path), subject

    def test_the_stopping_phrase_branch_is_unaffected(self, tmp_path):
        """The acceptance case routes through the stopping branch, not the
        progressive one — proof the tense requirement did not blind it."""
        assert _advises("stop captured session output persisting credentials", tmp_path)

    @pytest.mark.parametrize("subject", KNOWN_RESIDUE_SUBJECTS)
    def test_the_KNOWN_residue_still_fires(self, subject, tmp_path):
        """Documented limits, pinned so closing one is visible rather than silent.

        Two faces of one problem, both requiring a parser rather than a regex:
        a past participle after a PRESENT auxiliary ("are now stored") reads as
        past, and `logs`/`stores`/`writes` are nouns in one sentence and verbs in
        the next. ERE has no negative lookbehind, and dropping the ambiguous
        forms would take the true positives with them — "no longer stores API
        keys" is precisely what the stopping branch is for.

        On an ADVISORY axis measuring 0/1737 over this repo's own subjects, that
        residue is cheaper than a hand-rolled grammar classifier, which is an
        open-set claim about English and the shape that does not converge.

        If one of these starts failing, the residue was closed: drop it from
        KNOWN_RESIDUE_SUBJECTS, move it to BENIGN_SUBJECTS, and update the hook's
        KNOWN LIMIT notes.
        """
        assert _advises(subject + "\n", tmp_path), subject


class TestTheFireRateGateActuallyRunsInCI:
    """A skipped gate is an unevaluated gate.

    `test_fire_rate_on_this_repositorys_real_subjects` skips rather than lies on
    a shallow clone — correct behaviour, but under the default depth-1 checkout
    that meant the 0.1% bar never ran in CI at all, so a later pattern change
    with a bad rate would have passed. The measurement needs history, so the job
    that runs it must check out history (Codex P2, PR #1623).
    """

    def test_the_blocking_test_job_checks_out_full_history(self):
        """Asserted against the PARSED workflow, not its text.

        A substring check is satisfied by the explanatory comment that sits two
        lines above the setting — measured: deleting the real `with:` block left
        a text-based version of this test green. Read the step.
        """
        import yaml

        wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
        steps = wf["jobs"]["test"]["steps"]
        checkout = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
        assert checkout, "the `test` job does not check out the repository"
        depths = [(s.get("with") or {}).get("fetch-depth") for s in checkout]
        assert 0 in depths, (
            "the blocking `test` job checks out shallow (fetch-depth "
            f"{depths}), so every history-derived measurement in the suite "
            "silently SKIPS instead of gating"
        )


class TestTheVersionLedgerIsAppendOnly:
    """A removed hash wedges the hook OFF on every install still carrying it.

    `sync-hooks.sh` treats an installed hash that is absent from the ledger as
    USER-MODIFIED and skips the update — so dropping the immediate parent's hash
    means the new advisory, and every later commit-msg change, never deploys
    there. The ledger's own header records this happening once already
    (`commit-msg:f239fd3e` was found live-wedged on this install), which is why
    the backfill section exists at all.
    """

    LEDGER = REPO / ".genesis-hook-versions"

    @staticmethod
    def _hashes(text: str) -> set[str]:
        return {
            line.strip()
            for line in text.split("\n")
            if line.strip() and not line.lstrip().startswith("#")
        }

    def test_no_hash_this_repo_ever_shipped_has_been_removed(self):
        """Checked against the ledger's OWN history, not against `origin/main`.

        Two reasons, and the second is why this does not skip. Every revision
        that ever touched the file is a version some install may still be
        carrying, so the union over history is the set that matters — the tip of
        one branch is a strictly weaker check. And `origin/main` is a REMOTE ref
        that a CI checkout does not reliably create, so keying on it meant this
        guard skipped exactly where it was needed; a skip here is also not free,
        because the suite's dormancy ceiling is pinned exact-fit.
        """
        revs = subprocess.run(
            # --full-history so a merge does not prune a side branch that touched
            # the file (default simplification drops TREESAME parents), --follow
            # so a rename does not truncate the history at the rename point.
            # Both matter for a union that claims to be "every version shipped".
            [
                "git", "-C", str(REPO), "log", "--full-history", "--follow",
                "--format=%H", "--", ".genesis-hook-versions",
            ],
            capture_output=True,
            text=True,
        )
        assert revs.returncode == 0, f"git log failed: {revs.stderr}"

        ever_shipped: set[str] = set()
        for rev in [r for r in revs.stdout.split("\n") if r.strip()]:
            blob = subprocess.run(
                ["git", "-C", str(REPO), "show", f"{rev}:.genesis-hook-versions"],
                capture_output=True,
                text=True,
            )
            if blob.returncode == 0:
                ever_shipped |= self._hashes(blob.stdout)

        assert ever_shipped, "no ledger revisions readable — the check would be vacuous"
        dropped = ever_shipped - self._hashes(self.LEDGER.read_text())
        assert not dropped, (
            "hashes removed from the ledger — every install carrying one of these "
            f"stops receiving hook updates: {sorted(dropped)}"
        )

    def test_the_current_commit_msg_hash_is_recorded(self):
        """The other direction: an unrecorded CURRENT hash means a fresh install
        gets the hook and then treats it as user-modified on the next update."""
        import hashlib

        digest = hashlib.sha256(HOOK.read_bytes()).hexdigest()
        assert f"commit-msg:{digest}" in self.LEDGER.read_text(), (
            "the shipped commit-msg hook's hash is not in the ledger"
        )
