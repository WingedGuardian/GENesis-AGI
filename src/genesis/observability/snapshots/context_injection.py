"""Context-injection health: is a hook's output actually reaching the model?

The Claude Code harness persists a hook's stdout above an UNDOCUMENTED size
threshold: the full output goes to a file under the session's ``tool-results/``
directory and the model receives a ~2 KB preview. Nothing errors. The session
simply runs without whatever that hook was carrying — and nothing anywhere
says so.

MEASURED on this install (2026-08-30) by TWO different instruments, so the
denominators differ and are stated separately rather than blended into one
figure that belongs to neither:

  * counting FILES under the CC projects tree — 143 persisted hook outputs
    across 58 sessions, spanning a MONTH before anyone noticed;
  * scanning TRANSCRIPTS — across 387 transcripts, all 195 persistence
    wrappers found were SessionStart, every one the identity/charter/EK
    injection.

The
threshold also MOVES ACROSS VERSIONS (it dropped sharply in a CC update
mid-window, tripling the filing rate overnight; whether it can move without a
version bump is unverified), so no budget constant in any hook can be trusted
to stay correct indefinitely.

This collector therefore watches GROUND TRUTH — the harness's own persisted
files — rather than our arithmetic: a fresh ``hook-*-stdout.txt`` IS the
harness saying "I withheld a hook's output from the model", independent of
every assumption in every emitter. It never reads an emitter's constant, which
is what keeps it right when that constant is wrong.

Three things it deliberately does NOT do. It does not filter filings down to
the one hook we fixed — a filed proactive-memory or guard output is also a real
loss, with a different remedy, and reporting only what we recognise would
recreate the silence. It holds no state about emitters: the per-part markers
this used to read were deleted when the emitters gained a hard chokepoint,
because a marker could only ever record a condition that is now unreachable,
while the two conditions it never covered (the cap moving, another hook filing)
are exactly what this sees. And it never carries a byte of another hook's
CONTENT into its own observation — see :func:`_attribute`.

**Every read here reports its own failure.** A watcher for silent loss that can
itself go quiet is worthless, so directory traversal records unreadable paths
in :attr:`InjectionHealth.errors` rather than letting them vanish into an empty
listing. ``Path.glob`` was replaced for exactly this reason: it SWALLOWS the
traversal ``OSError`` and yields nothing, so an unreadable session subtree is
indistinguishable from a clean one.

Shape mirrors ``deploy_health.py``: sync filesystem work behind an async
entry, pure facts -> findings derivation, no side effects.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import re
import stat as stat_module
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from genesis.cc.types import cc_project_key
from genesis.env import repo_root

# Our own cap-measurement probe writes deliberately oversized hook output (see
# GENESIS_CTX_PROBE_BYTES in scripts/genesis_session_context.py). Those filings
# are expected artifacts of measuring, not losses — 17 of 27 filings in one
# window were probe runs. Detected by content, because there is nothing else to
# distinguish them by.
_PROBE_SENTINEL = b"PROBE-START"

#: The probe emitter's closed shape (genesis_session_context.py, the
#: GENESIS_CTX_PROBE_BYTES seam): `PROBE-START <one repeated filler char>
#: PROBE-END`. The filler alphabet is the emitter's, verbatim — "A", or "é"
#: in multibyte mode.
_PROBE_FILLERS = (b"A", "\u00e9".encode())
_PROBE_TAIL = b" PROBE-END"


def _probe_shaped(head: bytes) -> bool:
    """Whether ``head`` is the probe emitter's OWN closed shape — not merely
    its prefix.

    The probe branch is the only one that DROPS a filing from the findings, so
    it must authenticate more than a public literal: ``PROBE-START`` appears in
    this repo and in the watcher's own alert text, so an oversized recall
    payload quoting it first would otherwise be silently excluded — the exact
    loss this collector exists to report, suppressed by its own suppression
    branch. The real emitter writes ``PROBE-START `` + one repeated filler
    character + `` PROBE-END`` and nothing else, so require exactly that.

    ``head`` is the first :data:`_HEAD_BYTES` bytes. When it is SHORTER than
    the window we hold the whole file and the closing marker must be present;
    at exactly the window size the tail may be out of view, so only the filler
    run is checked (a multibyte filler may be split mid-character at the cut —
    "is a prefix of the filler repeated" covers that).
    """
    prefix = _PROBE_SENTINEL + b" "
    if not head.startswith(prefix):
        return False
    body = head[len(prefix):]
    if body.endswith(_PROBE_TAIL):
        body = body[: -len(_PROBE_TAIL)]
    elif len(head) < _HEAD_BYTES:
        return False  # whole file in view, no closing marker: not our probe
    return any(
        (f * (len(body) // len(f) + 1)).startswith(body) for f in _PROBE_FILLERS
    )

#: Label for a filing we cannot attribute to a known producer. The findings name
#: that filing's PATH instead of quoting it — see :func:`_attribute`.
OTHER_HOOK = "other hook"

#: Label for a filing we could not open at all.
UNREADABLE_FILING = "unreadable filing"

#: Everything outside the shape a real filing path takes. The leaf filename is
#: chosen by the process that wrote it, so it is escaped before it enters an
#: observation — see :func:`_safe_path`.
_PATH_UNSAFE = re.compile(r"[^\w./~+-]")


def _safe_path(path: object) -> str:
    """Escape a path on its way into an observation.

    EVERY string this module derives from the filesystem passes through here or
    :func:`_safe_reason` — the escaping is applied at the boundary rather than
    at the one call site that happens to render a path today.

    Why it exists: the project, session and leaf names all come from a tree
    written by OTHER processes, and POSIX permits every byte but ``/`` and NUL
    in a filename, newlines included. The result lands in an
    ``infrastructure_alert`` whose source ``memory/provenance.py`` stamps
    ``first_party``, and ``db/crud/observations.py`` stores ``content``
    verbatim — so an unescaped newline lets a filename forge what reads as an
    additional finding line in Genesis's own voice.

    MEASURED: a filing named ``hook-EVIL\\nINJECTED FINDING LINE-stdout.txt``
    put a raw newline into the DEGRADED finding. The first version of this fix
    escaped only the "FILED" line and left the error path unescaped — the same
    value, one boundary over. Hence a helper, used everywhere, instead.
    """
    return _PATH_UNSAFE.sub("?", str(path))


def _safe_text(text: object) -> str:
    """Escape PROSE read off disk on its way into an observation.

    Separate from :func:`_safe_path` because the two have different shapes and
    using the path escaper on prose is a bug in the other direction: it strips
    spaces and parentheses, so ``no --part argument (settings.json out of
    date)`` came back as ``no?--part?argument...`` — caught by an existing test
    rather than by reading, which is why the split exists.

    Keeps every printable character (spaces included) and replaces the rest.
    ``str.isprintable()`` is False for newlines, carriage returns, tabs, C0/C1
    controls and the Unicode line/paragraph separators — which is exactly the
    set that can forge a line break in a rendered finding.
    """
    return "".join(c if c.isprintable() else "?" for c in str(text))


def _safe_reason(exc: BaseException) -> str:
    """The reason an OS call failed, WITHOUT its embedded copy of the path.

    ``str(OSError)`` renders as ``[Errno 13] Permission denied: '<path>'`` — a
    second, unescaped copy of the filename inside the exception text. Taking
    ``strerror`` alone keeps the diagnosis and drops the copy; the caller has
    already rendered the path through :func:`_safe_path`.

    Escaped with :func:`_safe_text`, not :func:`_safe_path`. ``strerror`` is
    PROSE — "Permission denied", "Input/output error" — and the path escaper
    eats its spaces, so this rendered "Permission?denied" into the very
    `critical` alert the watcher exists to send. That is the same mistake the
    mis-wire boundary six lines away had already been fixed for, applied at one
    site and not its sibling: the pattern this whole review round was about.
    ``strerror`` is libc text and cannot contain a newline, so preserving
    printables costs nothing.
    """
    reason = getattr(exc, "strerror", None) or type(exc).__name__
    return _safe_text(reason)


#: Labels that name no producer. Every one of these gets its PATH rendered:
#: the remedy tells the operator to open the file, so a finding that withholds
#: the path is an instruction that cannot be followed.
_UNATTRIBUTED = frozenset({OTHER_HOOK, UNREADABLE_FILING})

#: How much of the append-only mis-wire log to tail. Generous next to a line
#: (~90 bytes) and constant regardless of how long the file has been growing.
_MISWIRE_TAIL_BYTES = 64_000

#: A slug prefix shorter than this cannot meaningfully scope anything.
_MIN_SLUG_PREFIX = 8

#: How much of a filed file to read when attributing it to a producer.
_HEAD_BYTES = 240

#: The session-context emitter stamps this on the first line of every part, so
#: attribution is a CONTRACT rather than a guess about what a file starts with.
_PART_STAMP = re.compile(rb"\[genesis-ctx:([\w-]+)")

#: The part names that stamp is allowed to name. A closed set, because the
#: captured group is bytes from a file this process does not own: an unknown
#: name means the file is NOT ours, whatever it claims. Kept in parity with
#: ``_PARTS`` in scripts/genesis_session_context.py by a test.
_KNOWN_PARTS = frozenset({"charter", "identity-core", "identity-user", "knowledge", "all"})

#: The pre-stamp session-context injection always began with this heading.
#: Kept ONLY so filings from sessions started before the fix are attributed
#: correctly during the restart window; new filings carry the stamp above.
_LEGACY_SESSION_CONTEXT_HEAD = b"## Session Configuration"

# The projects tree accumulates a directory per session forever, so the scan is
# bounded. Bounded, NOT silently: hitting it sets `scan_truncated` and
# derive_findings says so — a cap that hides what it dropped reads as "all
# clear", which is the failure this collector exists to catch. Applied AFTER
# the lookback filter, so a backlog of old files can never crowd out a fresh
# incident (which would be a silent all-clear wearing a truncation notice).
_MAX_FRESH = 2_000

#: How many distinct read failures to name in the findings before summarising.
_MAX_LISTED_ERRORS = 3

#: How many affected session ids the restart remedy names before summarising.
#: SEPARATE from the filings' ``max_listed`` (5) on purpose: the session list is
#: an INVENTORY the operator acts on ("restart THESE"), not a sample, so it must
#: name every session in the ordinary case — 8 sessions filing at once was the
#: live 2026-09-05 shape, and inheriting the filings' 5 would leave 3 unnamed
#: under a "restart the affected sessions" remedy. 20 sits well above that while
#: bounding a pathological fan-out. Past it, the EXACT total is stated alongside
#: the first 20 names — a selection with a denominator, not a silent cut. It does
#: NOT claim the "filing paths above" cover the rest: that list caps at
#: ``max_listed`` (5), so beyond 20 sessions the overflow is genuinely
#: un-enumerated, and the honest thing is to say the count, not point at a list
#: that does not hold them. 20 named + an exact total is enough for the operator
#: to grasp the scale; a >20-session fan-out is itself the "cap MOVED" escalation
#: the remedy already names.
_MAX_SESSIONS_NAMED = 20

#: Hard bound on the recorded errors themselves (the display bound above is a
#: separate, smaller one). One entry per unreadable path, and the whole list is
#: hashed into the alert identity, so an unbounded list is unbounded work on a
#: check that runs hourly.
_MAX_ERRORS = 50

#: Marks the single overflow entry `bound_errors` appends when it truncates.
#: ONE home, because two places depend on it: the writer that renders it and
#: `alert_identity`, which must EXCLUDE it — it carries a count, and a count in
#: the identity re-pages a standing condition every time the count moves.
_ERROR_OVERFLOW_PREFIX = "…and "


def _default_projects_dirs() -> tuple[Path, ...]:
    """Every tree that may hold this install's CC session data.

    A UNION rather than a choice. ``CLAUDE_CONFIG_DIR`` relocates Claude Code's
    data root — ``onboarding/floor.py`` already treats it as authoritative when
    locating ``.credentials.json``, which is a sibling of ``projects/`` — but
    that ``projects/`` follows it is UNVERIFIED here (no install on this box
    sets the variable, so there is nothing to measure). Scanning both costs a
    listing of a directory that usually does not exist; scanning only one and
    guessing wrong costs the silence this watcher exists to break.
    """
    roots = [Path.home() / ".claude"]
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    seen: dict[Path, None] = {}
    for r in roots:
        # Resolved before dedup: a configured root that is a SYMLINK to the
        # default, or a relative path, compares unequal by `Path` alone — and
        # the same filings would then be counted twice, producing an alert
        # whose filing count disagrees with its session count. A relative value
        # also resolves against THIS process's CWD (the awareness server's, not
        # the hook's); resolving makes that explicit rather than silent.
        # strict=False so an absent root still dedups — the scan probes it.
        candidate = r / "projects"
        with contextlib.suppress(OSError):
            candidate = candidate.resolve()
        seen.setdefault(candidate, None)
    return tuple(seen)


def _default_miswire_log() -> Path:
    return Path.home() / ".genesis" / "session_awareness" / "context_miswire.log"


def _genesis_sessions_dir() -> Path:
    """Genesis's OWN per-session state — the evidence that CC runs on this box.

    Used only to tell a blind scan from an empty one (:func:`_note_blind_scan`).
    """
    return Path.home() / ".genesis" / "sessions"


@dataclass
class InjectionHealth:
    """Facts gathered from the filesystem; findings derived separately.

    THE ESCAPING CHOKEPOINT. Values arrive through :meth:`add_error` and
    :meth:`note_filing`, which escape them once, so nothing downstream has to
    remember to. ``derive_findings`` can interpolate freely because a raw value
    cannot be in here.

    That is not decoration. Escaping-by-convention produced the P1 and the
    CRITICAL of this review cycle: an unrecognised hook's output was quoted into
    an observation ``memory/provenance.py`` stamps ``first_party``, and then —
    after that was fixed at the rendering boundary — a filename containing a
    NEWLINE reached the same observation through the ERROR list, one boundary
    over, where it could forge what reads as an extra finding line in Genesis's
    own voice. Same value, same destination, two sites, fixed separately.
    """

    filing_sessions: int = 0
    probe_filings: int = 0  # excluded from findings; reported for honesty
    foreign_filings: int = 0  # outside any Genesis checkout; counted, not alerted
    miswires: list[str] = field(default_factory=list)  # fresh mis-wired invocations
    scan_truncated: bool = False  # hit _MAX_FRESH — reported, never silent
    # Private so the only way in is through the escaping methods below. A test
    # asserts nothing appends to them directly.
    _errors: list[str] = field(default_factory=list, repr=False)
    _filings: list[dict] = field(default_factory=list, repr=False)

    @property
    def errors(self) -> list[str]:
        """Every read that FAILED, already escaped."""
        return self._errors

    @property
    def fresh_filings(self) -> list[dict]:
        """``{path, size, age_h, producer}`` per filing, paths already escaped."""
        return self._filings

    @property
    def filing_session_ids(self) -> list[str]:
        """Distinct sessions that filed, first-seen order, ids already escaped.

        Over ALL producers — this is the "across N session(s)" population.
        Derived from the per-filing ``session`` fact rather than stored, so it
        cannot drift from ``_filings``. The restart remedy scopes a NARROWER
        list (session-context producers only); see :func:`_session_context_sessions`.
        """
        return list(dict.fromkeys(d["session"] for d in self._filings))

    def add_error(self, subject: object, what: str, exc: BaseException | None = None) -> None:
        """Record a read that failed. Escapes at the boundary, every time.

        ``subject`` is usually a path chosen by another process; ``what`` is our
        own fixed prose; ``exc`` supplies the reason WITHOUT its embedded second
        copy of the path (``str(OSError)`` renders one).
        """
        detail = f": {_safe_reason(exc)}" if exc is not None else ""
        self._errors.append(f"{_safe_path(subject)} {what}{detail}")

    def note_filing(self, path: Path, *, size: int, age_h: float, producer: str) -> None:
        """Record a filing. The path is escaped here so no renderer has to.

        The session id (``<projects>/<slug>/<SESSION>/tool-results/<file>`` — the
        grandparent's name) rides in the row too, escaped at this same boundary:
        it is CC-authored filesystem metadata, POSIX permits a newline in it, and
        it now reaches a first_party observation via the restart remedy. Kept as
        a FACT on each filing rather than a pre-derived list, so each consumer
        scopes it correctly — the distinct-session COUNT is over all producers,
        but the restart remedy names only the session-context subset (a session
        that filed only an other-hook output has a different remedy, not restart).
        """
        self._filings.append(
            {
                "path": _safe_path(path),
                "size": size,
                "age_h": age_h,
                "producer": producer,
                "session": _safe_path(path.parent.parent.name),
            }
        )

    def note_miswire(self, reason: str) -> None:
        """Record a mis-wire reason read off disk. PROSE, so escaped as prose."""
        self.miswires.append(_safe_text(reason) or "unknown reason")

    def bound_errors(self, limit: int) -> None:
        """Cap the error list, announcing what was dropped.

        Bounded because ``alert_identity`` hashes the whole list and one entry
        is recorded per unreadable path. Never SILENT: a cap that hides what it
        dropped reads as an all-clear, which is the failure this module exists
        to catch.
        """
        if len(self._errors) > limit:
            dropped = len(self._errors) - limit
            self._errors[limit:] = [f"{_ERROR_OVERFLOW_PREFIX}{dropped} more unreadable path(s)"]


def _slug(path: Path) -> str:
    """CC's project-directory slug for a filesystem path.

    Delegates to the repo's canonical encoder rather than re-deriving it. The
    local version replaced only ``/`` and ``.``, which is right for THIS
    install's paths and wrong in general: CC replaces EVERY non-alphanumeric
    character, so a checkout path containing an underscore or a space produced
    a slug that matched no real directory — and every main-checkout filing then
    scored "foreign" while the watcher reported healthy.
    """
    return cc_project_key(str(path))


def _main_checkout(path: Path) -> Path:
    """Normalise a worktree path to the main checkout that contains it.

    ``repo_root()`` resolves relative to the importing code, so a process
    running from a worktree reports the WORKTREE as the root — and scoping on
    that alone would silently ignore every filing in the main checkout.
    MEASURED while building this: run from a worktree, the scan scored the 9
    live main-tree filings "foreign" and reported all-clear. Too broad is a
    counted extra; too narrow is the silence this watcher exists to break.
    """
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i : i + 2] == (".claude", "worktrees"):
            return Path(*parts[:i])
    return path


def _genesis_slug_prefixes(health: InjectionHealth | None = None) -> tuple[str, ...]:
    """Slug prefixes that belong to THIS install.

    Scoping matters: the projects tree holds every project the operator has
    ever opened, and an unscoped watcher pages `critical` because someone's
    unrelated repo filed a hook — an alarm that cries about other people's
    software gets muted, taking ours with it. Derived from paths rather than a
    subprocess (`git worktree list`) so the check stays side-effect-free: every
    Genesis worktree lives under the main checkout or under ~/.genesis, so the
    two prefixes cover all of them, background-sessions included.
    """
    prefixes = {_slug(Path.home() / ".genesis")}
    try:
        prefixes.add(_slug(_main_checkout(repo_root())))
    except Exception as exc:
        # Recorded, not merely fallen back from. Every other failure in this
        # module reports itself; this was the last read that did not, and a
        # guessed scope is exactly the state in which the watcher reports
        # "healthy" about the wrong tree.
        if health is not None:
            health.add_error("repo root", "unresolvable, scope GUESSED", exc)
        prefixes.add(_slug(Path.home() / "genesis"))
    # Drop a degenerate prefix. `repo_root()` honours GENESIS_REPO_ROOT verbatim,
    # so a relative or `/` value can normalise to `Path()` whose slug is "-" —
    # and EVERY CC project slug starts with "-", which would silently widen the
    # scan to every repo the operator has ever opened and page critical about
    # someone else's software.
    return tuple(sorted(p for p in prefixes if len(p) >= _MIN_SLUG_PREFIX))


def _in_scope(slug: str, prefixes: tuple[str, ...]) -> bool:
    """Does ``slug`` name a project inside this install?

    Boundary-aware: a bare ``startswith`` would pull in a sibling whose name
    merely EXTENDS ours (a ``~/.genesis-unrelated`` checkout slugs to
    ``…--genesis-unrelated``, which starts with the ``~/.genesis`` prefix), and
    that project's filings would then be counted as ours.
    The slug separator is ``-``, so require an exact match or a separator next.

    LIMIT, stated rather than papered over: CC's slug mapping is lossy — every
    non-alphanumeric character becomes ``-``, so a SIBLING at ``<root>-name``
    is indistinguishable from a child directory ``<root>/name``. This check
    therefore excludes only a slug that shares the prefix without a separator;
    a same-prefixed sibling still reads as in-scope. That is the safe
    direction: too broad costs a reported filing (noise), too narrow costs
    silence, which is what this watcher exists to break.
    """
    return any(slug == p or slug.startswith(p + "-") for p in prefixes)


class _Reads:
    """THE FILESYSTEM CHOKEPOINT. Every read this collector makes goes here.

    Not a wrapper for tidiness — a place where "did you remember to report the
    failure" cannot be asked. Three defects in one review cycle were reads that
    failed and told nobody, each found and fixed on its own: ``Path.glob``
    swallowing a traversal ``OSError`` (an unreadable subtree read as clean); a
    per-file ``stat`` swallowed with ``continue`` (a directory at mode 0o444 is
    LISTABLE but not TRAVERSABLE, so every filing under it vanished and the
    caller RESOLVED a live critical alert as "within budget"); and
    ``Path.exists()``/``is_dir()``, which do not swallow at all — they RAISE on
    EACCES, into a handler that silenced the entire watcher. Fixing three
    instances left a fourth possible. This makes all of them impossible.

    Every method returns a value and never raises. Failures go to
    :meth:`InjectionHealth.add_error`, which escapes them.

    Locked by test_no_unguarded_filesystem_access_in_the_collector.
    """

    def __init__(self, health: InjectionHealth) -> None:
        self._health = health
        self._report = True

    @contextlib.contextmanager
    def unreported(self):
        """Traverse without reporting — for trees outside this install.

        An unreadable directory in somebody else's project is not our alarm,
        and an alarm that cries about other people's software gets muted,
        taking ours with it. Scoped by a context manager rather than a per-call
        flag, so "did you pass errors=None" is not a question either.
        """
        prior, self._report = self._report, False
        try:
            yield
        finally:
            self._report = prior

    def _fail(self, path: object, what: str, exc: BaseException) -> None:
        if self._report:
            self._health.add_error(path, what, exc)

    def listdir(self, path: Path) -> list[Path]:
        """Entries of ``path``, SORTED. Absent / not-a-directory are NOT failures.

        Sorted because ``iterdir`` returns filesystem order: a watcher whose
        output reorders while its subject has not changed reads as noise, and
        the nondeterminism also made one test pass by directory-order luck.
        """
        try:
            return sorted(path.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as exc:
            self._fail(path, "is not readable", exc)
            return []

    def stat(self, path: Path) -> os.stat_result | None:
        try:
            return path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._fail(path, "could not be stat'd", exc)
            return None

    def is_dir(self, path: Path) -> tuple[bool, bool]:
        """``(is_directory, exists)``. Never raises, unlike ``Path.is_dir()``."""
        st = self.stat(path)
        if st is None:
            return (False, False)
        return (stat_module.S_ISDIR(st.st_mode), True)

    def head(self, path: Path, n: int) -> bytes | None:
        try:
            with path.open("rb") as fh:
                return fh.read(n)
        except OSError as exc:
            self._fail(path, "could not be read for attribution", exc)
            return None

    def tail_text(self, path: Path, n: int) -> str | None:
        """The last ``n`` bytes as text, or None if absent/unreadable.

        No ``exists()`` precheck, deliberately: that call RAISES on EACCES, and
        this is the FIRST read the collector makes — a raise here took the
        filings scan down with it. ``open`` already distinguishes absent from
        unreadable, so the precheck bought nothing and cost the whole scan.
        """
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - n))
                return fh.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._fail(path, "is not readable", exc)
            return None


def _probe_root(root: Path, reads: _Reads, health: InjectionHealth) -> bool:
    """Is ``root`` a usable scan root? Records the reason when it is not."""
    is_dir, exists = reads.is_dir(root)
    if not exists:
        return False  # a fresh install has no sessions yet — absence is not a failure
    if not is_dir:
        # Probed at the ROOT only. Inside the tree a non-directory is ordinary
        # (stray files live beside project dirs) and `listdir` skips it
        # silently; a non-directory WHERE THE ROOT SHOULD BE is a
        # misconfiguration that would otherwise scan zero files and read as
        # all-clear.
        health.add_error(root, "exists but is not a directory")
        return False
    return True


def _genesis_saw_recent_session(reads: _Reads, cutoff: float) -> bool:
    """Did THIS install's own session store record a live CC session in-window?

    ``last_prompt_time`` is rewritten on every prompt of every foreground
    session, so a fresh mtime here is Genesis's own evidence that CC ran —
    independent of CC's directory layout, which is the whole point: it is the
    half of the freshness comparison that a CC update cannot move.

    Reads are REPORTED, deliberately NOT wrapped in ``unreported()``. Absent
    files — dispatched sessions with no ``last_prompt_time`` — are already
    silent: ``_Reads.stat`` maps FileNotFoundError to None. What is left to
    report is a genuine I/O failure on THIS install's own store (a permission
    fault, an unreadable subtree), which must not read as "no fresh prompt":
    that answer is indistinguishable from an idle install and would let a
    fossil CC tree resolve a live alert (the fourth-granularity blind resolve
    this probe exists to catch). Suppressing here traded that failure for
    silence; the primitives already give absence for free, so the wrapper only
    hid the case worth reporting.
    """
    for d in reads.listdir(_genesis_sessions_dir()):
        st = reads.stat(d / "last_prompt_time")
        if st is not None and st.st_mtime >= cutoff:
            return True
    return False


def _note_blind_scan(
    projects_dirs: tuple[Path, ...], reads: _Reads, health: InjectionHealth, *, what: str
) -> None:
    """NO scan root was usable — is that an all-clear, or blindness?

    "Could not look" and "looked and found nothing wrong" are different readings,
    and conflating them is the exact failure this module exists to catch. Note
    the boundary: a root that EXISTS and is empty was genuinely inspected and is
    a true all-clear; this fires only when there was nowhere to look at all.

    CC's data root is an UNDOCUMENTED internal, and this watcher was built
    because such internals move. Relocate it and the scan would otherwise
    produce zero filings, zero errors, and an active RESOLVE of a live critical
    alert with "no fresh filings; injection within budget" — the alarm switching
    itself off at precisely the moment it went blind.

    Discriminated on Genesis's OWN state rather than a second guess at CC's
    layout, which is the point: another guess would share the first one's blind
    spot. If this install has session directories of its own, the SessionStart
    hook has been running inside CC windows, so CC is in use here and an empty
    scan is a BLIND scan. With no session state either, CC has genuinely never
    run on this box and silence is the correct reading.
    """
    # COUPLING, deliberately named: `scripts/disk_hygiene.sh` prunes this
    # directory at 60 days, so that threshold is load-bearing for THIS check too
    # — empty it and every blindness report below turns back into a silent
    # all-clear. 60d is far past any live session, so the two do not collide
    # today; lowering it without reading this is how they would.
    if not reads.listdir(_genesis_sessions_dir()):
        return  # CC has never run here — nothing to be blind to
    extra = f" (of {len(projects_dirs)} configured)" if len(projects_dirs) > 1 else ""
    health.add_error(
        projects_dirs[0] if projects_dirs else "(no scan root configured)",
        f"{what}{extra}, yet this install HAS session state of its own — the "
        "watcher had nothing in view, so this reading is not an all-clear "
        "(CC's data root or its project-slug encoding may have moved)",
    )


def _attribute(path: Path, reads: _Reads) -> tuple[str, bool]:
    """``(producer, is_probe)`` for a filed hook output, read from its head.

    The returned label is drawn from a CLOSED SET that this module authors. No
    byte of the file's content reaches the caller, and that is a security
    property rather than a stylistic one: the label ends up in an
    ``infrastructure_alert`` observation, which ``memory/provenance.py``
    classifies ``first_party`` — so anything quoted here would pass the
    trusted-only ``SAFE_SURFACING_ORIGINS`` filters that reflection and
    perception use to keep external text out of Genesis's own reasoning.
    An earlier version quoted the first 80 characters of an unrecognised hook's
    output, sanitised and framed "verbatim head (unverified)"; stripping
    control characters does not change where the text CAME from, and the frame
    does not travel with the string. The filing's PATH carries the same
    diagnostic value — it is Genesis-observed filesystem metadata — and
    :func:`derive_findings` names it so the operator can read the file directly.
    """
    head = reads.head(path, _HEAD_BYTES)
    if head is None:
        # Both an unattributed filing AND a failed read: `reads` has already
        # recorded the second, so the reading is marked degraded rather than
        # passing as authoritative with one odd row in it.
        return (UNREADABLE_FILING, False)
    # ANCHORED, like the part stamp below and for the stronger reason: this is
    # the only branch that DROPS a filing from the findings entirely, so an
    # unanchored match is a fail-open — any hook merely MENTIONING the sentinel
    # in its first bytes would suppress a real loss, where a mis-read stamp only
    # picks the wrong remedy. The probe writes it at byte 0 by construction.
    if _probe_shaped(head):
        return ("cap-measurement probe", True)
    # match, not search: the emitter guarantees the stamp is at byte 0 (the
    # recovery header is the first thing `_begin_part` writes). Searching the
    # whole 240-byte head would attribute ANY hook that merely MENTIONS the
    # marker — a recall injection quoting this very incident is the realistic
    # instance — and `derive_findings` would then suppress the other-hook
    # remedy and tell the operator to restart sessions instead of bounding that
    # hook's output. A closed set matched at an arbitrary offset is not closed.
    stamp = _PART_STAMP.match(head)
    if stamp:
        part = stamp.group(1).decode("ascii", "replace")
        # Closed set: the capture is bytes from a file we do not own, so a name
        # we do not recognise means this is not our emitter imitating us.
        if part in _KNOWN_PARTS:
            return (f"session-context part '{part}'", False)
    if head.startswith(_LEGACY_SESSION_CONTEXT_HEAD):
        # Transitional, and deliberately the ONLY content signature here. Every
        # filing predating the stamp begins with this line (MEASURED: 9 of 9
        # live filings, and every non-probe filing in 387 transcripts), and a
        # session started before the fix keeps emitting the old shape until it
        # restarts — so without this the whole post-deploy window would be
        # attributed to "some other hook" and given the wrong remedy.
        return ("session-context (pre-stamp emitter — session predates the fix)", False)
    return (OTHER_HOOK, False)


def _read_miswires(path: Path, cutoff: float, reads: _Reads, health: InjectionHealth) -> None:
    """Record fresh mis-wire reasons. Append-only file; nothing clears it.

    A mis-wired hook emits only the charter part, which stays UNDER the cap by
    design — so the harness never files it and the filings scan cannot see it.
    This log is therefore the ONLY out-of-band evidence that the condition
    exists, which is why an unreadable log is reported rather than read as
    "no mis-wires": that silence let a later tick resolve a live critical alert
    as healthy, having lost its only witness.

    Tail-read because the log is append-only by design; reading it whole would
    grow with the file forever on a check that runs every hour.
    """
    blob = reads.tail_text(path, _MISWIRE_TAIL_BYTES)
    if blob is None:
        return
    for line in blob.splitlines()[-200:]:
        ts, _, reason = line.partition("\t")
        try:
            when = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            continue
        if when >= cutoff:
            health.note_miswire(reason.strip())


def _collect_sync(
    projects_dirs: tuple[Path, ...],
    lookback_hours: float,
    now: float,
    miswire_log: Path | None = None,
) -> InjectionHealth:
    health = InjectionHealth()
    reads = _Reads(health)
    cutoff = now - lookback_hours * 3600
    prefixes = _genesis_slug_prefixes(health)

    _read_miswires(miswire_log or _default_miswire_log(), cutoff, reads, health)

    fresh: list[tuple[float, Path, int]] = []
    roots_usable = 0
    in_scope_projects = 0
    tool_results_seen = 0
    fresh_in_scope_entries = 0
    for root in projects_dirs:
        if not _probe_root(root, reads, health):
            continue
        roots_usable += 1
        for project in reads.listdir(root):
            in_scope = _in_scope(project.name, prefixes)
            in_scope_projects += int(in_scope)
            # Out-of-scope trees are still traversed (foreign_filings is an
            # honesty counter) but their read failures are not ours to report.
            with contextlib.ExitStack() as scope:
                if not in_scope:
                    scope.enter_context(reads.unreported())
                for session in reads.listdir(project):
                    if in_scope:
                        # Freshness evidence for the FOURTH blindness granularity
                        # below. A session entry is a transcript file or a session
                        # directory; a live session appends to its transcript, so
                        # its mtime moves. Unreported: the stat only feeds the
                        # coverage discriminator, and any real read failure on
                        # this same entry is reported by the listdir/stat a few
                        # lines down.
                        with reads.unreported():
                            st_e = reads.stat(session)
                        if st_e is not None and st_e.st_mtime >= cutoff:
                            fresh_in_scope_entries += 1
                    tool_results = session / "tool-results"
                    if in_scope:
                        # UNREPORTED: this probe only feeds the blindness counter
                        # below, and the `listdir` on the very same path reports
                        # any real read failure a line later. Without this, an
                        # unreadable tree records every path TWICE and the
                        # operator-facing error list doubles for one cause.
                        with reads.unreported():
                            tool_results_seen += int(reads.is_dir(tool_results)[0])
                    for f in reads.listdir(tool_results):
                        if not (f.name.startswith("hook-") and f.name.endswith("-stdout.txt")):
                            continue
                        st = reads.stat(f)
                        if st is None:
                            continue
                        if st.st_mtime < cutoff:
                            continue  # lookback FIRST — the cap bounds fresh files only
                        if not in_scope:
                            health.foreign_filings += 1
                            continue
                        # This file's OWN size. It used to be read off the loop
                        # variable after the scan finished, so every filing
                        # reported the size of whichever file was visited last
                        # — including one skipped as stale or foreign.
                        fresh.append((st.st_mtime, f, st.st_size))

    # THREE granularities of the SAME blindness, one per CC-owned convention
    # this scan rests on. A root that vanished is the obvious one; a root that is
    # fine while nothing inside it matches this install's slugs is the quiet one
    # (the slug encoding belongs to CC); and a matching project tree that holds
    # no `tool-results` directory at all is the quietest — that directory NAME is
    # also CC's, and if it is renamed the traversal still succeeds, finds
    # nothing, and `derive_findings` returns an all-clear that RESOLVES a live
    # alert. That is the module's own generator — a verifier that cannot tell "I
    # did not check" from "I checked and it is fine" — one layer below where the
    # first two granularities close it. Either way the scan had nothing in view,
    # and a scan with nothing in view must not resolve anything.
    #
    # The leaf pattern (`hook-*-stdout.txt`) is deliberately NOT covered here: an
    # in-scope session legitimately holds a `tool-results` directory with no hook
    # filings in it, which is the HEALTHY state and must stay distinguishable
    # from blindness. Naming the limit rather than guessing at it.
    if not roots_usable:
        _note_blind_scan(projects_dirs, reads, health, what="was not a usable scan root")
    elif not in_scope_projects:
        _note_blind_scan(
            projects_dirs, reads, health, what="held no project directory belonging to this install"
        )
    elif not tool_results_seen:
        _note_blind_scan(
            projects_dirs,
            reads,
            health,
            what="held no tool-results directory under any session of this install",
        )
    elif not fresh_in_scope_entries and _genesis_saw_recent_session(reads, cutoff):
        # The three checks above are EXISTENCE checks, and CC's projects tree is
        # retained indefinitely — so after a CC update moves new sessions to a
        # different slug or layout, the fossil directories keep all three
        # satisfied forever while every current session runs outside the scan.
        # The discriminator that catches it without paging a quiet install:
        # Genesis's own store says a session was live inside the lookback, yet
        # NOTHING under any in-scope project moved in that window. Both fresh →
        # healthy; both stale → idle install, healthy; Genesis fresh + CC view
        # fossil → the scan is looking at the past, and must not resolve.
        _note_blind_scan(
            projects_dirs,
            reads,
            health,
            what=(
                "showed no session activity inside the lookback under any in-scope "
                "project, while this install's own session store recorded a live session"
            ),
        )

    fresh.sort(key=lambda t: t[0], reverse=True)  # newest first
    if len(fresh) > _MAX_FRESH:
        health.scan_truncated = True
        fresh = fresh[:_MAX_FRESH]

    for mtime, f, size in fresh:
        producer, is_probe = _attribute(f, reads)
        if is_probe:
            health.probe_filings += 1
            continue
        health.note_filing(f, size=size, age_h=round((now - mtime) / 3600, 1), producer=producer)
    # note_filing tracks the distinct affected sessions (escaped, deduped); the
    # count is just its length — no second, unescaped set to drift from it.
    health.filing_sessions = len(health.filing_session_ids)

    # Applied LAST, after attribution has had its chance to record failures.
    health.bound_errors(_MAX_ERRORS)
    return health


#: Fields deliberately absent from :func:`alert_identity`, each with the reason.
#: A field that is neither keyed nor exempted fails the coverage test, so adding
#: state to :class:`InjectionHealth` forces a conscious decision about whether a
#: change in it should re-alert.
_IDENTITY_EXEMPT_FIELDS: dict[str, str] = {
    # Folded in as its STABLE projection (presence + producer set). The dicts
    # themselves carry sizes and ages that move every tick, so hashing them
    # would mint a fresh critical alert hourly for one standing incident.
    "_filings": "keyed by presence and producer set instead",
    # A count over the SAME rolling window, and it drifts for the same reason:
    # sessions age out of the lookback with no new incident. Presence of filings
    # and the producer set carry the condition; the count is reported in the
    # finding text, where it informs without re-paging.
    "filing_sessions": "a rolling-window count — reported, never keyed",
    # The session ids the restart remedy names now ride inside `_filings` (a
    # per-filing `session` fact), covered by `_filings`'s exemption above — so
    # they need no separate entry here. Their re-page behaviour is a DELIBERATE
    # tradeoff, documented at the remedy: keying the session SET into the alert
    # identity would re-page on every session change (the churn this exemption
    # avoids), while exempting it means a sustained-churn window can show a
    # stale "restart THESE" list until the alert resolves. Post-deploy the
    # session-context population only SHRINKS (a restarted session stops filing;
    # new sessions carry the fixed wiring and never file), so the list ages to
    # empty and the alert resolves — the frozen-list case needs NEW sessions to
    # start filing, which only happens if the cap MOVED, and the remedy already
    # escalates to that. Accepted with that reasoning rather than re-paging.
    # Not rendered by derive_findings, and both move for reasons unrelated to
    # any loss: probes appear while measuring the cap, foreign filings whenever
    # the operator opens another repo.
    "probe_filings": "not rendered in findings",
    "foreign_filings": "not rendered in findings",
}


def alert_identity(health: InjectionHealth) -> str:
    """A stable key over every state :func:`derive_findings` can report.

    Lives here rather than at the alerting call site because this module owns
    the state: a caller assembling the key by hand omits a field the moment one
    is added, and the failure is silent — ``supersede_except_hash`` keeps the
    OLD alert and ``skip_if_duplicate`` drops the new content, so a genuinely
    new condition (a mis-wire appearing beside an unchanged filing count)
    never reaches the operator while the alert looks live.

    Keyed on the CONDITION, never on a tally. Every count here is taken over a
    ROLLING lookback and re-evaluated hourly, so filings age out on their own and
    the tally moves with no new incident. Keyed by count, one standing incident
    minted a fresh alert on ~8 of the next 24 ticks — each one superseding the
    last and re-pushing at `critical`, which is the Telegram path. An alarm that
    repeats itself for a condition the operator has already seen is how the
    channel gets muted, and this module's whole argument is that a muted alarm
    takes ours down with it.

    So: presence rather than counts, plus the PRODUCER SET, because a new
    producer is a genuinely different remedy. The numbers still reach the
    operator — `derive_findings` renders them in the alert body; they simply do
    not decide whether to re-page.
    """
    producers = ",".join(sorted({str(d.get("producer", "?")) for d in health.fresh_filings}))
    return (
        f"filings:{'yes' if health.fresh_filings else 'no'}"
        f":by:{producers}"
        f":miswired:{'yes' if health.miswires else 'no'}"
        f":truncated:{health.scan_truncated}"
        # The overflow line `bound_errors` appends carries a COUNT, and a count
        # in the identity is exactly what this function exists to keep out: two
        # ticks at 52 and 53 unreadable paths produce a byte-identical alert body
        # with a different hash, so `supersede_except_hash` re-pages at
        # `critical` — the Telegram path — for one standing condition. The bound
        # was added for hashing cost and quietly reintroduced the failure the
        # docstring above argues against. Hash the real errors, and carry the
        # overflow as presence, not as a tally.
        f":errors:{'|'.join(sorted(e for e in health.errors if not e.startswith(_ERROR_OVERFLOW_PREFIX)))}"
        f":errors_bounded:"
        f"{'yes' if any(e.startswith(_ERROR_OVERFLOW_PREFIX) for e in health.errors) else 'no'}"
    )


def identity_covered_fields() -> tuple[str, ...]:
    """Field names :func:`alert_identity` is expected to cover. For the test."""
    return tuple(
        f.name for f in dataclasses.fields(InjectionHealth) if f.name not in _IDENTITY_EXEMPT_FIELDS
    )


def derive_findings(health: InjectionHealth, *, max_listed: int = 5) -> list[str]:
    """Facts -> human-readable findings. Pure; empty list = healthy."""
    findings: list[str] = []
    if health.errors:
        shown = "; ".join(health.errors[:_MAX_LISTED_ERRORS])
        extra = (
            f" (and {len(health.errors) - _MAX_LISTED_ERRORS} more)"
            if len(health.errors) > _MAX_LISTED_ERRORS
            else ""
        )
        findings.append(
            f"context-injection watcher DEGRADED — {shown}{extra}. It could not read "
            "everything it watches, so THIS READING CANNOT BE TREATED AS ALL-CLEAR."
        )
    if health.scan_truncated:
        findings.append(
            f"context-injection watcher stopped after {_MAX_FRESH} FRESH filings — this "
            "reading is partial and the real count may be higher."
        )
    if health.miswires:
        findings.append(
            f"the SessionStart hook was MIS-WIRED {len(health.miswires)} time(s) "
            f"({health.miswires[-1]}) — those sessions got the charter part ONLY, so "
            "identity and essential knowledge were missing from them. Fix the four "
            "`--part` entries in .claude/settings.json and restart those sessions."
        )
    if not health.fresh_filings:
        return findings

    total = len(health.fresh_filings)
    by_producer: dict[str, int] = {}
    for d in health.fresh_filings:
        by_producer[d["producer"]] = by_producer.get(d["producer"], 0) + 1
    listed = ", ".join(_render_filing(d) for d in health.fresh_filings[:max_listed])
    more = f" …and {total - max_listed} more" if total > max_listed else ""
    findings.append(
        f"the harness FILED {total} hook output(s) across {health.filing_sessions} "
        f"session(s) — those windows ran WITHOUT the filed content: {listed}{more}."
    )
    if any(p.startswith("session-context") for p in by_producer):
        # SCOPED to session-context filings: a session that filed only an
        # other-hook output has a different remedy (bound that hook), so naming
        # it here under "restart" would be wrong and would inflate the count.
        ids = list(
            dict.fromkeys(
                d["session"]
                for d in health.fresh_filings
                if d["producer"].startswith("session-context")
            )
        )
        named = ", ".join(ids[:_MAX_SESSIONS_NAMED])
        overflow = (
            f" …and {len(ids) - _MAX_SESSIONS_NAMED} more of {len(ids)} (the count is "
            "exact; only the first names are listed)"
            if len(ids) > _MAX_SESSIONS_NAMED
            else ""
        )
        findings.append(
            "session-context filings mean the injection crossed the harness cap: "
            f"RESTART the affected sessions ({len(ids)} total) [{named}{overflow}] "
            "(a session started before a fix keeps the old wiring until it does), "
            "and if filings continue after a restart the cap itself has MOVED — "
            "re-measure it with the probe seam in scripts/genesis_session_context.py "
            "and re-fit the part budgets."
        )
    if any(not p.startswith(("session-context", "cap-measurement")) for p in by_producer):
        findings.append(
            "filings from other hooks mean THAT hook's contribution was withheld from "
            "the model for those turns (a recall injection, a guard advisory) — read "
            "the path named above to see which hook it was, then bound its output via "
            "scripts/hooks/hook_output.py."
        )
    return findings


def _render_filing(d: dict) -> str:
    """One filing, for the findings line.

    An unattributed filing names its PATH: that is where the diagnostic value
    went when the head excerpt was removed (see :func:`_attribute`), and it is
    what the remedy asks the operator to open. ANY label naming no producer
    qualifies — an exact check against one of them told the operator to "read
    the path named above" for a filing whose path was never printed.

    The path is filesystem metadata Genesis observed, not file content — but
    the leaf ``hook-<id>-stdout.txt`` was named by the writing process, and
    POSIX permits every byte but ``/`` and NUL there, newlines included. That is
    far weaker than the 80-char content excerpt this replaced (one line, ~255
    bytes, no payload), yet ``memory/provenance.py`` now rests a first_party
    claim on this string, so it is escaped rather than trusted.

    It is escaped ONCE, by :meth:`InjectionHealth.note_filing`, which is why
    this interpolates ``d["path"]`` directly. This function used to escape it a
    SECOND time. That was not free defence in depth: with two layers, deleting
    either one left the whole suite green, so no test pinned either — the very
    shape the ingestion chokepoint exists to delete. Measured by mutation: with
    the second escape here, reverting ``note_filing``'s reddened 0 tests.
    """
    base = f"{d['producer']}: {d['size']} B ({d['age_h']} h ago)"
    if d["producer"] not in _UNATTRIBUTED:
        return base
    return f"{base} at {d['path']}"


async def context_injection(
    *,
    projects_dir: Path | None = None,
    lookback_hours: float = 24.0,
    now: float | None = None,
    miswire_log: Path | None = None,
) -> InjectionHealth:
    """Async entry: gather filesystem facts off the event loop."""
    return await asyncio.to_thread(
        _collect_sync,
        (projects_dir,) if projects_dir is not None else _default_projects_dirs(),
        lookback_hours,
        now if now is not None else time.time(),
        miswire_log,
    )
