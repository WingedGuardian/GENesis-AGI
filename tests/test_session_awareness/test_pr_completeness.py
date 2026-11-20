"""Structural report checks; synthetic reports never prove independent execution."""

import copy
import json

import pytest

from genesis.session_awareness.pr_evidence import EvidenceError, parse_evidence
from genesis.session_awareness.pr_verification_sources import PrSourceSnapshot, SourceError
from tests.test_session_awareness.test_pr_verification_sources import AFTER_BLOB, patch


def candidate():
    return dict(
        repo="owner/repo",
        pr=7,
        merge_commit="a" * 40,
        deploy=dict(method="content", established=True, detail="owned source fixture"),
        claims=[
            dict(
                claim="Text changes from before to after",
                verdict="pass",
                tier="MEASURED",
                measurement="Owned positive after; identical probe on before fails",
            )
        ],
        controls=["The same assertion fails on before"],
        gaps=[],
        findings=[],
        e2e=[dict(step="verify the changed text", claim=0)],
    )


def snapshot(tmp_path):
    return PrSourceSnapshot(
        repo="owner/repo",
        pr=7,
        merge_commit="a" * 40,
        head="b" * 40,
        base="c" * 40,
        body="Changes ordinary text.\nE2E: verify the changed text",
        diff=patch(tmp_path).decode(),
        files=[
            dict(
                filename="ordinary.txt", additions=1, deletions=1, status="modified", sha=AFTER_BLOB
            )
        ],
        acquired_at="2026-01-01T00:00:00+00:00",
    )


def reviewed(tmp_path):
    from genesis.session_awareness.pr_completeness import (
        ReviewInventory,
        evidence_sha256,
        inventory_sha256,
    )

    source = snapshot(tmp_path)
    packet = source.packet()
    lines = source.diff.split("\n")
    line = next(i + 1 for i, text in enumerate(lines) if text == "+after")
    hunk = next(text for text in lines if text.startswith("@@"))
    inventory = dict(
        policy_version=1,
        source={
            key: packet[key]
            for key in (
                "repo",
                "pr",
                "merge_commit",
                "head",
                "base",
                "body_sha256",
                "diff_sha256",
                "files_sha256",
            )
        },
        items=[
            dict(
                id="text-change",
                statement="Changes the ordinary text from before to after",
                sources=[
                    dict(kind="body", start=1, quote="Changes ordinary text."),
                    dict(kind="diff", path="ordinary.txt", start=line, hunk=hunk, quote="+after"),
                ],
            )
        ],
        files=[dict(path="ordinary.txt", items=["text-change"])],
        e2e_steps=["verify the changed text"],
    )
    raw = candidate()
    raw["completeness"] = dict(
        policy_version=1,
        review_id="synthetic-review",
        review_execution_ref="fixture-only:no-model-ran",
        reviewer_kind="fresh_context_agent",
        completed_at="2026-01-01T00:00:00+00:00",
        inventory=inventory,
        inventory_sha256=inventory_sha256(ReviewInventory.model_validate(inventory)),
        evidence_sha256=evidence_sha256(parse_evidence(raw)),
        coverage=[dict(item="text-change", claims=[0], gaps=[])],
        claim_reviews=[
            dict(
                claim=0,
                kind="runtime",
                controls=[0],
                contrast="Identical assertion accepts after and rejects before",
            )
        ],
        findings=[],
        complete=True,
    )
    return raw, source


def test_optional_report_preserves_legacy_serialization():
    doc = parse_evidence(candidate())
    assert "completeness" in type(doc).model_fields
    assert doc.completeness is None
    assert "completeness" not in doc.model_dump(mode="json")
    assert "completeness" not in parse_evidence(candidate() | {"completeness": None}).model_dump(
        mode="json"
    )


def test_complete_synthetic_report_is_current_but_not_execution_proof(tmp_path):
    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    assessment = assess_completeness(parse_evidence(raw), source)
    assert assessment.valid and assessment.status == "verified"
    assert parse_evidence(raw).completeness.review_execution_ref == "fixture-only:no-model-ran"


def test_evidence_hash_binds_every_semantic_field_except_report():
    import hashlib

    from genesis.session_awareness.pr_completeness import evidence_sha256

    raw = candidate()
    doc = parse_evidence(raw)
    expected = hashlib.sha256(
        json.dumps(
            doc.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert evidence_sha256(doc) == expected
    assert evidence_sha256(parse_evidence(json.loads(json.dumps(raw, sort_keys=True)))) == expected
    for field, value in [
        ("no_progress", True),
        ("controls", ["different opposite arm"]),
        ("merge_commit", "e" * 40),
        ("tracking", [dict(kind="accepted_limit", target="gaps/0", rationale="fixture")]),
    ]:
        assert evidence_sha256(parse_evidence(raw | {field: value})) != expected


@pytest.mark.parametrize("change", ["evidence", "body", "diff", "file", "merge", "inventory"])
def test_any_bound_input_change_refuses(tmp_path, change):
    from dataclasses import replace

    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    assert assess_completeness(parse_evidence(raw), source).valid
    if change == "evidence":
        raw["claims"][0]["measurement"] += " changed"
    elif change == "inventory":
        raw["completeness"]["inventory"]["items"][0]["statement"] += " changed"
    elif change == "file":
        source = replace(source, files=[source.files[0] | {"sha": "e" * 40}])
    elif change == "body":
        source = replace(source, body=source.body + "\nAnother independent promise.")
    else:
        field = "merge_commit" if change == "merge" else change
        source = replace(
            source,
            **{field: "e" * 40 if change == "merge" else getattr(source, field) + " changed"},
        )
    result = assess_completeness(parse_evidence(raw), source)
    assert not result.valid and result.status == "stale"


@pytest.mark.parametrize("value", [False, True, -1, 1, "0"])
def test_claim_indices_are_strict_and_zero_based(tmp_path, value):
    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    assert assess_completeness(parse_evidence(raw), source).valid
    raw["completeness"]["coverage"][0]["claims"] = [value]
    with pytest.raises(EvidenceError, match="claims"):
        parse_evidence(raw)


def test_missing_report_remains_parseable_and_is_not_current(tmp_path):
    from genesis.session_awareness.pr_completeness import assess_completeness

    result = assess_completeness(parse_evidence(candidate()), snapshot(tmp_path))
    assert not result.valid and result.status == "missing"


def test_packet_reader_checks_digests_and_complete_git_census(tmp_path):
    from genesis.session_awareness.pr_completeness import load_packet
    from genesis.session_awareness.pr_verification_sources import (
        canonical_json,
        digest,
        save_packet,
    )

    source = snapshot(tmp_path)
    directory = tmp_path / "packet"
    target = save_packet(source, directory)
    assert load_packet(directory).packet() == source.packet()
    altered = copy.deepcopy(source.packet())
    altered["diff"] = ""
    altered["diff_sha256"] = digest(b"")  # Self-consistent hashes cannot hide an absent diff.
    target.write_text(canonical_json(altered))
    with pytest.raises(SourceError, match="diff"):
        load_packet(directory)


def refresh(raw):
    from genesis.session_awareness.pr_completeness import (
        ReviewInventory,
        evidence_sha256,
        inventory_sha256,
    )

    review = raw.pop("completeness")
    review["evidence_sha256"] = evidence_sha256(parse_evidence(raw))
    review["inventory_sha256"] = inventory_sha256(
        ReviewInventory.model_validate(review["inventory"])
    )
    raw["completeness"] = review


@pytest.mark.parametrize(
    "change",
    [
        "missing-file",
        "duplicate-item",
        "unknown-file-item",
        "missing-coverage",
        "empty-coverage",
        "extra-claim",
        "extra-gap",
        "unused-control",
        "wrong-body-line",
        "wrong-diff-path",
        "wrong-hunk",
        "missing-e2e",
    ],
)
def test_internally_rehashed_incomplete_reports_cannot_pass(tmp_path, change):
    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    assert assess_completeness(parse_evidence(raw), source).valid
    report = raw["completeness"]
    inventory = report["inventory"]
    if change == "missing-file":
        inventory["files"] = []
    elif change == "duplicate-item":
        inventory["items"] *= 2
    elif change == "unknown-file-item":
        inventory["files"][0]["items"] = ["unknown"]
    elif change == "missing-coverage":
        report["coverage"] = []
    elif change == "empty-coverage":
        report["coverage"][0]["claims"] = []
    elif change == "extra-claim":
        raw["claims"].append(dict(raw["claims"][0], claim="an unsupported extra promise"))
    elif change == "extra-gap":
        raw["gaps"] = [dict(kind="method", detail="unmapped extra limitation")]
    elif change == "unused-control":
        raw["controls"].append("a source read cannot silently pass as an unaudited control")
    elif change == "wrong-body-line":
        inventory["items"][0]["sources"][0]["start"] = 999
    elif change == "wrong-diff-path":
        inventory["items"][0]["sources"][1]["path"] = "absent.txt"
    elif change == "wrong-hunk":
        inventory["items"][0]["sources"][1]["hunk"] = "@@ -99 +99 @@"
    else:
        inventory["e2e_steps"] = []
    refresh(raw)
    result = assess_completeness(parse_evidence(raw), source)
    assert not result.valid and result.status == "inconsistent"


@pytest.mark.parametrize("kind", ["read", "method_gap"])
def test_inspection_and_explicit_method_gap_remain_valid(tmp_path, kind):
    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    raw["controls"] = []
    audit = dict(
        claim=0,
        kind=kind,
        inspection_basis="owned artifact read; runtime contrast unavailable here",
    )
    if kind == "read":
        raw["claims"][0]["tier"] = "READ"
    else:
        audit["method_gap"] = 0
        raw["gaps"] = [dict(kind="method", detail="the opposite environment is unavailable")]
        raw["completeness"]["coverage"][0]["gaps"] = [0]
    raw["completeness"]["claim_reviews"] = [audit]
    refresh(raw)
    assert assess_completeness(parse_evidence(raw), source).valid
    if kind == "read":
        raw["claims"][0]["tier"] = "MEASURED"
    else:
        raw["gaps"][0]["kind"] = "owner_only"
    refresh(raw)
    assert not assess_completeness(parse_evidence(raw), source).valid


@pytest.mark.parametrize(
    "change",
    [
        "unknown-version",
        "bool-version",
        "missing-version",
        "bad-time",
        "duplicate-index",
        "empty-controls",
        "wrong-control-kind",
        "unknown-field",
    ],
)
def test_malformed_optional_report_is_a_schema_error(tmp_path, change):
    raw, _ = reviewed(tmp_path)
    report = raw["completeness"]
    if change == "unknown-version":
        report["policy_version"] = 2
    elif change == "bool-version":
        report["policy_version"] = True
    elif change == "missing-version":
        del report["policy_version"]
    elif change == "bad-time":
        report["completed_at"] = "2026-01-01T00:00:00"
    elif change == "duplicate-index":
        report["coverage"][0]["claims"] = [0, 0]
    elif change == "empty-controls":
        report["claim_reviews"][0]["controls"] = []
    elif change == "wrong-control-kind":
        report["claim_reviews"][0]["kind"] = "read"
    else:
        report["unexpected_field"] = "must not be silently discarded"
    with pytest.raises(EvidenceError):
        parse_evidence(raw)


@pytest.mark.parametrize(
    "complete,findings",
    [(False, []), (True, [dict(detail="unresolved promise", items=["text-change"])])],
)
def test_complete_flag_cannot_override_failed_review(tmp_path, complete, findings):
    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    raw["completeness"].update(complete=complete, findings=findings)
    result = assess_completeness(parse_evidence(raw), source)
    assert not result.valid and result.status == "failed"


def test_real_check_cli_preserves_inputs_and_requires_retained_inventory(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from genesis.session_awareness.pr_completeness import ReviewInventory, inventory_sha256
    from genesis.session_awareness.pr_verification_sources import save_packet

    raw, source = reviewed(tmp_path)
    directory = tmp_path / "packet"
    packet = save_packet(source, directory)
    inventory = tmp_path / "stage-a.json"
    inventory.write_text(json.dumps(raw["completeness"]["inventory"]))
    evidence = tmp_path / "candidate.json"
    evidence.write_text(json.dumps(raw))
    script = Path(__file__).resolve().parents[2] / "scripts/verification_review.py"
    command = [
        sys.executable,
        "-I",
        str(script),
        "check",
        "--packet",
        str(directory),
        "--inventory",
        str(inventory),
    ]
    before = {path: path.read_bytes() for path in (packet, inventory, evidence)}
    inventory_only = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert inventory_only.returncode == 0, inventory_only.stderr
    assert "semantic review remains unverified" in inventory_only.stdout
    checked = subprocess.run(
        command + ["--evidence", str(evidence)], capture_output=True, text=True, timeout=30
    )
    assert checked.returncode == 0, checked.stderr
    assert "current bindings verified" in checked.stdout
    assert "live source freshness was not checked" in checked.stdout
    assert {path: path.read_bytes() for path in before} == before
    raw["completeness"]["inventory"]["items"][0]["statement"] += " rewritten after Stage A"
    raw["completeness"]["inventory_sha256"] = inventory_sha256(
        ReviewInventory.model_validate(raw["completeness"]["inventory"])
    )
    evidence.write_text(json.dumps(raw))
    refused = subprocess.run(
        command + ["--evidence", str(evidence)], capture_output=True, text=True, timeout=30
    )
    assert refused.returncode == 2 and "retained Stage-A" in refused.stderr
    assert packet.read_bytes() == before[packet] and inventory.read_bytes() == before[inventory]


def test_input_byte_limits_and_partial_packet_refuse(tmp_path, monkeypatch):
    from genesis.session_awareness import pr_completeness as review
    from genesis.session_awareness.pr_verification_sources import save_packet

    source = snapshot(tmp_path)
    directory = tmp_path / "packet"
    directory.mkdir()
    with pytest.raises(SourceError, match="regular file"):
        review.load_packet(directory)
    directory.rmdir()
    save_packet(source, directory)
    monkeypatch.setattr(review, "MAX_PACKET_BYTES", 8)
    with pytest.raises(SourceError, match="byte limit"):
        review.load_packet(directory)


@pytest.mark.parametrize("field", ["quote", "path", "statement"])
def test_unpaired_unicode_is_a_schema_refusal(tmp_path, field):
    raw, _ = reviewed(tmp_path)
    item = raw["completeness"]["inventory"]["items"][0]
    if field == "statement":
        item[field] = "\ud800"
    else:
        item["sources"][0 if field == "quote" else 1][field] = "\ud800"
    with pytest.raises(EvidenceError):
        parse_evidence(raw)


def test_source_coordinates_use_lf_not_unicode_splitlines(tmp_path):
    from dataclasses import replace

    from genesis.session_awareness.pr_completeness import assess_completeness, source_binding

    raw, source = reviewed(tmp_path)
    source = replace(source, body="Changes ordinary\x85 text.\nE2E: verify the changed text")
    inventory = raw["completeness"]["inventory"]
    inventory["source"] = source_binding(source).model_dump(mode="json")
    inventory["items"][0]["sources"][0]["quote"] = "Changes ordinary\x85 text."
    refresh(raw)
    assert assess_completeness(parse_evidence(raw), source).valid


def test_oversized_report_is_refused_without_truncation(tmp_path):
    from genesis.session_awareness.pr_completeness import load_evidence

    raw, _ = reviewed(tmp_path)
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(raw))
    assert load_evidence(path).completeness is not None
    raw["completeness"].update(
        complete=False, findings=[dict(detail="x" * 3000, items=["text-change"])] * 100
    )
    parse_evidence(raw)  # Valid structure; the stored-evidence bound is separate.
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    with pytest.raises(SourceError, match="byte limit"):
        load_evidence(path)
    assert path.read_bytes() == before


def test_canonical_defaults_also_count_toward_the_evidence_cap(tmp_path, monkeypatch):
    from genesis.session_awareness import pr_completeness as review
    from genesis.session_awareness.pr_verification_sources import canonical_json

    path = tmp_path / "candidate.json"
    raw = json.dumps(candidate(), separators=(",", ":")).encode()
    canonical = canonical_json(parse_evidence(candidate()).model_dump(mode="json")).encode()
    assert len(canonical) > len(raw)
    path.write_bytes(raw)
    monkeypatch.setattr(review, "MAX_REVIEW_BYTES", len(raw))
    with pytest.raises(SourceError, match="stored evidence"):
        review.load_evidence(path)


def review_cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts/verification_review.py"
    spec = importlib.util.spec_from_file_location("owned_review_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_candidate_digest_stays_out_of_source_packet(tmp_path, monkeypatch, capsys):
    from genesis.session_awareness.pr_completeness import evidence_sha256

    source = snapshot(tmp_path)
    cli = review_cli()
    monkeypatch.setattr(cli, "acquire_sources", lambda *args: source)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate()))
    target = tmp_path / "packet"
    assert (
        cli.main(
            [
                "prepare",
                "--repo",
                "owner/repo",
                "--pr",
                "7",
                "--output",
                str(target),
                "--evidence",
                str(candidate_path),
            ]
        )
        == 0
    )
    assert evidence_sha256(parse_evidence(candidate())) in capsys.readouterr().out
    packet = json.loads((target / "packet.json").read_text())
    assert "evidence_sha256" not in packet and "completeness" not in packet


def test_invalid_candidate_refuses_before_source_acquisition(tmp_path, monkeypatch):
    cli = review_cli()
    monkeypatch.setattr(
        cli,
        "acquire_sources",
        lambda *args: pytest.fail("invalid candidate reached source acquisition"),
    )
    path = tmp_path / "bad.json"
    path.write_text('{"incomplete"')
    target = tmp_path / "packet"
    assert (
        cli.main(
            [
                "prepare",
                "--repo",
                "owner/repo",
                "--pr",
                "7",
                "--output",
                str(target),
                "--evidence",
                str(path),
            ]
        )
        == 2
    )
    assert not target.exists()


@pytest.mark.parametrize("line", [True, 0, -1, "1"])
def test_source_line_indices_are_strict_and_one_based(tmp_path, line):
    raw, _ = reviewed(tmp_path)
    raw["completeness"]["inventory"]["items"][0]["sources"][0]["start"] = line
    with pytest.raises(EvidenceError):
        parse_evidence(raw)


def test_report_metadata_is_excluded_only_at_the_top_level(tmp_path):
    from genesis.session_awareness.pr_completeness import evidence_sha256

    raw, _ = reviewed(tmp_path)
    original = evidence_sha256(parse_evidence(raw))
    raw["completeness"]["review_id"] = "other-synthetic-review"
    raw["completeness"]["review_execution_ref"] = "fixture-only:still-no-model"
    assert evidence_sha256(parse_evidence(raw)) == original


def test_closing_evidence_missing_an_e2e_mapping_is_inconsistent(tmp_path):
    # Both close writers run check_e2e_coverage on a closing verdict; the
    # offline check must refuse what they would refuse.
    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    raw["e2e"] = []
    refresh(raw)
    assessment = assess_completeness(parse_evidence(raw), source)
    assert not assessment.valid and assessment.status == "inconsistent"
    assert "E2E coverage" in assessment.reason


def test_non_closing_evidence_needs_no_e2e_mapping(tmp_path):
    # The writers skip the E2E check when nothing closes; so does the checker.
    from genesis.session_awareness.pr_completeness import assess_completeness
    from genesis.session_awareness.pr_evidence import decide

    raw, source = reviewed(tmp_path)
    raw["claims"][0]["verdict"] = "fail"
    raw["e2e"] = []
    refresh(raw)
    decision = decide(parse_evidence(raw))
    assert not getattr(decision, "closing", False)
    assessment = assess_completeness(parse_evidence(raw), source)
    assert assessment.valid and assessment.status == "verified"


def test_evidence_cap_uses_the_writers_stored_serialization(tmp_path, monkeypatch):
    # A document whose compact form fits but whose stored (indented) form does
    # not would pass the check and then be refused by both writers.
    from genesis.session_awareness import pr_completeness as review
    from genesis.session_awareness.pr_evidence import stored_evidence_json
    from genesis.session_awareness.pr_verification_sources import canonical_json

    doc = parse_evidence(candidate())
    compact = len(canonical_json(doc.model_dump(mode="json")).encode())
    stored = len(stored_evidence_json(doc).encode("utf-8"))
    assert compact < stored
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate(), separators=(",", ":")))
    monkeypatch.setattr(review, "MAX_REVIEW_BYTES", stored - 1)
    with pytest.raises(SourceError, match="stored evidence"):
        review.load_evidence(path)
    monkeypatch.setattr(review, "MAX_REVIEW_BYTES", stored)
    assert review.load_evidence(path).repo == "owner/repo"


def test_prepare_keeps_one_deadline_across_both_passes(tmp_path, monkeypatch):
    # The second reconciliation pass gets only what is left of the overall
    # bound, so prepare never runs past it and publishes nothing when it is spent.
    source = snapshot(tmp_path)
    cli = review_cli()
    clock = iter([0.0, cli.ACQUISITION_SECONDS + 1.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cli, "acquire_sources", lambda *args: source)
    target = tmp_path / "packet"
    argv = ["prepare", "--repo", "owner/repo", "--pr", "7", "--output", str(target)]
    assert cli.main(argv) == 2
    assert not target.exists()


def test_e2e_refusal_names_positions_not_declaration_text(tmp_path):
    from genesis.session_awareness.pr_completeness import assess_completeness

    raw, source = reviewed(tmp_path)
    raw["e2e"] = []
    refresh(raw)
    assessment = assess_completeness(parse_evidence(raw), source)
    assert assessment.status == "inconsistent" and "E2E" in assessment.reason
    assert "verify the changed text" not in assessment.reason
    assert "1" in assessment.reason


def test_prepare_refuses_a_second_pass_that_ends_after_the_deadline(tmp_path, monkeypatch):
    # The pass starts in time; the clock moves past the bound only once it returns.
    source = snapshot(tmp_path)
    cli = review_cli()
    now = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cli, "acquire_sources", lambda *args: source)
    real = cli.reconcile_diff

    def late(*args, **kwargs):
        result = real(*args, **kwargs)
        now[0] = cli.ACQUISITION_SECONDS + 1.0
        return result

    monkeypatch.setattr(cli, "reconcile_diff", late)
    target = tmp_path / "packet"
    argv = ["prepare", "--repo", "owner/repo", "--pr", "7", "--output", str(target)]
    assert cli.main(argv) == 2
    assert not target.exists()


def test_inventory_accepts_an_e2e_plan_longer_than_ordinary_text(tmp_path):
    from genesis.session_awareness.pr_completeness import ReviewInventory

    raw, _ = reviewed(tmp_path)
    inventory = dict(raw["completeness"]["inventory"], e2e_steps=["x" * 5000])
    assert ReviewInventory.model_validate(inventory).e2e_steps == ["x" * 5000]
