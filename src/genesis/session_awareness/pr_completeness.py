"""Typed independent-review receipts and pure current-input binding checks.

These checks establish structure and binding, not that a separate reviewer ran
or that its judgments are true. Retained execution and qualification own that.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from genesis.db.crud.pr_verifications import MAX_EVIDENCE_BYTES
from genesis.session_awareness.e2e_declaration import e2e_steps
from genesis.session_awareness.pr_verification_sources import (
    ACQUISITION_SECONDS,
    MAX_DIFF,
    MAX_FILES,
    MAX_METADATA,
    PrSourceSnapshot,
    SourceError,
    _file,
    _json,
    canonical_json,
    digest,
    reconcile_diff,
)

if TYPE_CHECKING:
    from genesis.session_awareness.pr_evidence import EvidenceDocument

Text = Annotated[StrictStr, StringConstraints(min_length=1, max_length=4096, pattern=r"\S")]
# E2E plan identities come from the PR body, which is read up to 65,536 characters.
E2EStep = Annotated[StrictStr, StringConstraints(min_length=1, max_length=65536, pattern=r"\S")]
Quote = Annotated[StrictStr, StringConstraints(min_length=1, max_length=4096)]
PathText = Annotated[StrictStr, StringConstraints(min_length=1, max_length=MAX_METADATA)]
Identifier = Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
Sha = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Hash = Annotated[StrictStr, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Index = Annotated[StrictInt, Field(ge=0)]
Line = Annotated[StrictInt, Field(ge=1)]
Version = Annotated[StrictInt, Field(ge=1, le=1)]
MAX_REVIEW_BYTES = MAX_EVIDENCE_BYTES
# JSON may escape every source control byte as six ASCII bytes. Include the
# original source bounds plus body/identity overhead; never assume 8 MiB on disk.
MAX_PACKET_BYTES = 6 * (MAX_DIFF + MAX_METADATA + 65536) + 65536


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceBinding(_Model):
    repo: Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")]
    pr: Annotated[StrictInt, Field(gt=0)]
    merge_commit: Sha
    head: Sha
    base: Sha
    body_sha256: Hash
    diff_sha256: Hash
    files_sha256: Hash


class BodySource(_Model):
    kind: Literal["body"]
    start: Line
    end: Line | None = None
    quote: Quote


class DiffSource(_Model):
    kind: Literal["diff"]
    path: PathText
    start: Line
    end: Line | None = None
    quote: Quote
    hunk: Text | None = None


class BlobSource(_Model):
    kind: Literal["blob"]
    path: PathText
    sha: Sha


SourceReference = Annotated[BodySource | DiffSource | BlobSource, Field(discriminator="kind")]


class InventoryItem(_Model):
    id: Identifier
    statement: Text
    sources: list[SourceReference] = Field(min_length=1, max_length=MAX_FILES)


class FileDisposition(_Model):
    path: PathText
    items: list[Identifier] = Field(default_factory=list, max_length=MAX_FILES)
    no_behavior_reason: Text | None = None

    @model_validator(mode="after")
    def _one_disposition(self):
        if bool(self.items) == (self.no_behavior_reason is not None):
            raise ValueError("file needs item IDs or a no-behavior reason, exclusively")
        return self


class ReviewInventory(_Model):
    policy_version: Version
    source: SourceBinding
    items: list[InventoryItem] = Field(max_length=MAX_FILES)
    files: list[FileDisposition] = Field(max_length=MAX_FILES)
    e2e_steps: list[E2EStep] = Field(default_factory=list, max_length=MAX_FILES)


class ItemCoverage(_Model):
    item: Identifier
    claims: list[Index] = Field(default_factory=list, max_length=MAX_FILES)
    gaps: list[Index] = Field(default_factory=list, max_length=MAX_FILES)


class RuntimeControl(_Model):
    claim: Index
    kind: Literal["runtime"]
    controls: list[Index] = Field(min_length=1, max_length=MAX_FILES)
    contrast: Text


class ReadInspection(_Model):
    claim: Index
    kind: Literal["read"]
    inspection_basis: Text


class MethodControlGap(_Model):
    claim: Index
    kind: Literal["method_gap"]
    method_gap: Index
    inspection_basis: Text


class UnverifiedControl(_Model):
    claim: Index
    kind: Literal["unverified"]
    inspection_basis: Text


ClaimReview = Annotated[
    RuntimeControl | ReadInspection | MethodControlGap | UnverifiedControl,
    Field(discriminator="kind"),
]


class ReviewFinding(_Model):
    detail: Text
    sources: list[SourceReference] = Field(default_factory=list, max_length=MAX_FILES)
    items: list[Identifier] = Field(default_factory=list, max_length=MAX_FILES)
    claims: list[Index] = Field(default_factory=list, max_length=MAX_FILES)
    controls: list[Index] = Field(default_factory=list, max_length=MAX_FILES)


def _indices(values, size, label):
    if len(values) != len(set(values)) or any(value >= size for value in values):
        raise ValueError(f"completeness {label}: duplicate or out-of-range index")


class CompletenessReview(_Model):
    policy_version: Version
    review_id: Identifier
    review_execution_ref: Text
    reviewer_kind: Literal["fresh_context_agent"]
    completed_at: StrictStr
    inventory: ReviewInventory
    inventory_sha256: Hash
    evidence_sha256: Hash
    coverage: list[ItemCoverage] = Field(max_length=MAX_FILES)
    claim_reviews: list[ClaimReview] = Field(max_length=MAX_FILES)
    findings: list[ReviewFinding] = Field(max_length=MAX_FILES)
    complete: StrictBool

    @field_validator("completed_at")
    @classmethod
    def _aware_completion(cls, value):
        try:
            aware = datetime.fromisoformat(value).utcoffset() is not None
        except ValueError:
            aware = False
        if not aware:
            raise ValueError("review completion needs an explicit timezone")
        return value

    def validate_indices(self, doc: EvidenceDocument) -> None:
        for entry in self.coverage:
            _indices(entry.claims, len(doc.claims), "claims")
            _indices(entry.gaps, len(doc.gaps), "gaps")
        _indices([entry.claim for entry in self.claim_reviews], len(doc.claims), "claim reviews")
        for entry in self.claim_reviews:
            if isinstance(entry, RuntimeControl):
                _indices(entry.controls, len(doc.controls), "controls")
            if isinstance(entry, MethodControlGap):
                _indices([entry.method_gap], len(doc.gaps), "method gap")
        for entry in self.findings:
            _indices(entry.claims, len(doc.claims), "finding claims")
            _indices(entry.controls, len(doc.controls), "finding controls")


@dataclass(frozen=True)
class CompletenessAssessment:
    valid: bool
    reason: str
    status: Literal["missing", "stale", "failed", "inconsistent", "verified"]


def evidence_sha256(doc: EvidenceDocument) -> str:
    return digest(canonical_json(doc.model_dump(mode="json", exclude={"completeness"})).encode())


def inventory_sha256(inventory: ReviewInventory) -> str:
    return digest(canonical_json(inventory.model_dump(mode="json")).encode())


def source_binding(snapshot: PrSourceSnapshot) -> SourceBinding:
    if type(snapshot.packet_version) is not int or snapshot.packet_version != 1:
        raise SourceError("unsupported source packet version")
    packet = snapshot.packet()
    return SourceBinding.model_validate({key: packet[key] for key in SourceBinding.model_fields})


def _references(references, snapshot):
    files = {entry["filename"]: entry for entry in snapshot.files}
    body_lines = snapshot.body.split("\n")
    diff_lines = snapshot.diff.split("\n")
    for reference in references:
        if isinstance(reference, BlobSource):
            if reference.path not in files or reference.sha != files[reference.path]["sha"]:
                raise SourceError("source blob reference is not in the file census")
            continue
        lines = body_lines if reference.kind == "body" else diff_lines
        end = reference.end or reference.start
        if not reference.start <= end <= len(lines):
            raise SourceError("source reference line range is invalid")
        if reference.quote not in "\n".join(lines[reference.start - 1 : end]):
            raise SourceError("source reference quote does not match its exact lines")
        if isinstance(reference, DiffSource):
            if reference.path not in files:
                raise SourceError("diff reference path is absent from the census")
            if reference.hunk is not None and (
                not reference.hunk.startswith("@@") or reference.hunk not in lines[:end]
            ):
                raise SourceError("diff reference hunk is absent")
        # Path/hunk attribution and statement meaning remain reviewer judgments;
        # this is coordinate/quote validation, not a second Git path parser.


def validate_inventory(inventory: ReviewInventory, snapshot: PrSourceSnapshot) -> None:
    if inventory.source != source_binding(snapshot):
        raise SourceError("inventory source binding does not match the packet")
    ids = [entry.id for entry in inventory.items]
    paths = [entry.path for entry in inventory.files]
    if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
        raise SourceError("inventory has duplicate item IDs or file dispositions")
    if set(paths) != {entry["filename"] for entry in snapshot.files}:
        raise SourceError("inventory file dispositions do not cover the complete census")
    for entry in inventory.files:
        if len(entry.items) != len(set(entry.items)) or not set(entry.items) <= set(ids):
            raise SourceError("file disposition has duplicate or unknown inventory items")
    _references((reference for entry in inventory.items for reference in entry.sources), snapshot)
    if inventory.e2e_steps != e2e_steps(snapshot.body):
        raise SourceError("inventory E2E steps differ from the source declaration identities")


def _check_reviews(review, doc):
    controls = set()
    for entry in review.claim_reviews:
        claim = doc.claims[entry.claim]
        if isinstance(entry, RuntimeControl):
            controls.update(entry.controls)
        elif isinstance(entry, ReadInspection):
            if claim.tier != "READ":
                raise SourceError("non-runtime READ claim needs its inspection basis")
        elif isinstance(entry, MethodControlGap):
            if doc.gaps[entry.method_gap].kind != "method":
                raise SourceError("unavailable runtime control needs an explicit method gap")
        elif claim.tier in ("MEASURED", "READ"):
            raise SourceError("an established claim cannot use an unverified control exemption")
    if {entry.claim for entry in review.claim_reviews} != set(range(len(doc.claims))):
        raise SourceError("every evidence claim needs an independent control/inspection audit")
    if controls != set(range(len(doc.controls))):
        raise SourceError("every declared control needs a linked independent audit")


def _closing_e2e_refusal(doc: EvidenceDocument, snapshot: PrSourceSnapshot) -> str | None:
    """The close writers' E2E coverage rule, applied where they apply it.

    Both writers run ``check_e2e_coverage`` only on a closing decision
    (``validator_writes.py``, ``scripts/pr_verification.py``). Offline, the
    decision is the document's derived verdict with no caller inputs, so this
    is stricter than the writers in two cases, never looser: a validator who
    records with ``park`` (a derived pass-with-measured-gaps becomes
    cannot-verify), and incomplete tracking, which needs the database and makes
    the writers record the row open (tracking-pending) without this rule.
    """
    from genesis.session_awareness.pr_evidence import Decision, check_e2e_coverage, decide

    decision = decide(doc)
    if not isinstance(decision, Decision) or not decision.closing:
        return None
    if check_e2e_coverage(doc, snapshot.body) is None:
        return None
    # The same rule, reported by position: the writers' message quotes the
    # declarations, and the offline check never prints raw PR source.
    steps = e2e_steps(snapshot.body)
    mapped = {item.step.strip() for item in doc.e2e}
    missing = [str(i + 1) for i, step in enumerate(steps) if step not in mapped]
    stale = len(mapped - set(steps))
    return (
        f"E2E coverage is incomplete: declared step(s) {', '.join(missing) or 'none'} "
        f"unmapped, {stale} mapping(s) name no declared step"
    )


def assess_completeness(
    doc: EvidenceDocument, snapshot: PrSourceSnapshot
) -> CompletenessAssessment:
    review = doc.completeness
    if review is None:
        return CompletenessAssessment(
            False, "independent completeness review is missing", "missing"
        )
    from genesis.session_awareness.pr_recording import check_merge_commit

    try:
        if (doc.repo, doc.pr) != (snapshot.repo, snapshot.pr) or check_merge_commit(
            doc.merge_commit, snapshot.merge_commit
        ):
            return CompletenessAssessment(
                False, "evidence merge identity differs from sources", "stale"
            )
        if (
            review.inventory.source != source_binding(snapshot)
            or review.inventory_sha256 != inventory_sha256(review.inventory)
            or review.evidence_sha256 != evidence_sha256(doc)
        ):
            return CompletenessAssessment(
                False, "review source, inventory or evidence digest is stale", "stale"
            )
        if not review.complete or review.findings:
            return CompletenessAssessment(
                False, "independent review is incomplete or has unresolved findings", "failed"
            )
        validate_inventory(review.inventory, snapshot)
        review.validate_indices(doc)
        ids = {entry.id for entry in review.inventory.items}
        mapped = [entry.item for entry in review.coverage]
        if len(mapped) != len(set(mapped)) or set(mapped) != ids:
            raise SourceError("every inventory item needs exactly one coverage mapping")
        if any(not entry.claims and not entry.gaps for entry in review.coverage):
            raise SourceError("inventory promise has neither a claim nor an explicit gap")
        for name, targets in (("claims", doc.claims), ("gaps", doc.gaps)):
            if {index for entry in review.coverage for index in getattr(entry, name)} != set(
                range(len(targets))
            ):
                raise SourceError(f"every evidence {name} entry needs a source inventory mapping")
        _check_reviews(review, doc)
        refusal = _closing_e2e_refusal(doc, snapshot)
        if refusal is not None:
            return CompletenessAssessment(False, refusal, "inconsistent")
    except SourceError as exc:
        return CompletenessAssessment(False, str(exc), "inconsistent")
    except (ValueError, TypeError, KeyError, UnicodeError):
        # Never dump pydantic validation inputs or arbitrary object values.
        return CompletenessAssessment(
            False, "review structure or source references are inconsistent", "inconsistent"
        )
    return CompletenessAssessment(
        True, "current source/evidence/report bindings verified", "verified"
    )


def _load(path: Path, limit: int):
    if not path.is_file():
        raise SourceError("required review input is not a regular file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SourceError("review input exceeds its byte limit; not truncated")
    return _json(raw)


def load_inventory(path: Path) -> ReviewInventory:
    try:
        return ReviewInventory.model_validate(_load(path, MAX_REVIEW_BYTES))
    except ValidationError:
        raise SourceError("inventory schema is invalid") from None


def load_evidence(path: Path) -> EvidenceDocument:
    from genesis.session_awareness.pr_evidence import parse_evidence, stored_evidence_json

    doc = parse_evidence(_load(path, MAX_REVIEW_BYTES))
    # Measure what the close writers store, not the compact form: a document
    # that fits only compact would pass here and then be refused at recording.
    if len(stored_evidence_json(doc).encode("utf-8")) > MAX_REVIEW_BYTES:
        raise SourceError("stored evidence including review exceeds the byte limit")
    return doc


def load_packet(directory: Path) -> PrSourceSnapshot:
    data = _load(directory / "packet.json", MAX_PACKET_BYTES)
    names = {entry.name for entry in fields(PrSourceSnapshot)}
    try:
        if not isinstance(data, dict) or set(data) != names | {
            "body_sha256",
            "diff_sha256",
            "files_sha256",
        }:
            raise ValueError("packet fields")
        snapshot = PrSourceSnapshot(**{name: data[name] for name in names})
        if (
            not isinstance(snapshot.body, str)
            or len(snapshot.body) > 65536
            or not isinstance(snapshot.diff, str)
        ):
            raise ValueError("source text")
        if (
            len(snapshot.diff.encode()) > MAX_DIFF
            or type(snapshot.files) is not list
            or len(snapshot.files) > MAX_FILES
        ):
            raise ValueError("source bounds")
        if datetime.fromisoformat(snapshot.acquired_at).utcoffset() is None:
            raise ValueError("source time")
        cleaned = [_file(entry) for entry in snapshot.files]
        if (
            cleaned != snapshot.files
            or cleaned != sorted(cleaned, key=lambda entry: entry["filename"])
            or len(canonical_json(cleaned).encode()) > MAX_METADATA
        ):
            raise ValueError("file census")
        binding = source_binding(snapshot)
        for name in ("body_sha256", "diff_sha256", "files_sha256"):
            if data[name] != getattr(binding, name):
                raise ValueError("source digest")
        reconcile_diff(snapshot.diff.encode(), cleaned, timeout=ACQUISITION_SECONDS)
        return snapshot
    except SourceError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise SourceError(
            "source packet schema, digest or complete diff census is invalid"
        ) from None
