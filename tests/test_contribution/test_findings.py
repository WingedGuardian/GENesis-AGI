"""Unit tests for genesis.contribution.findings."""
from __future__ import annotations

from genesis.contribution.findings import (
    DivergenceResult,
    Finding,
    FindingKind,
    InstallInfo,
    ReviewResult,
    SanitizerResult,
    Severity,
    VersionGateResult,
)


def test_severity_is_str_enum():
    assert Severity.BLOCK == "block"
    assert Severity.WARN == "warn"
    assert Severity.INFO == "info"


def test_finding_kind_values():
    assert FindingKind.SECRET == "secret"
    assert FindingKind.FORBIDDEN_PATH == "forbidden_path"


def test_finding_to_dict_minimal():
    f = Finding(kind=FindingKind.SECRET, severity=Severity.BLOCK, message="x")
    d = f.to_dict()
    assert d["kind"] == "secret"
    assert d["severity"] == "block"
    assert d["message"] == "x"
    assert d["file"] is None
    assert d["line"] is None


def test_finding_to_dict_full():
    f = Finding(
        kind=FindingKind.PORTABILITY,
        severity=Severity.BLOCK,
        message="ip",
        file="a.py",
        line=42,
        scanner="portability",
        detail="10.1.2.3",
    )
    d = f.to_dict()
    assert d["file"] == "a.py"
    assert d["line"] == 42
    assert d["scanner"] == "portability"
    assert d["detail"] == "10.1.2.3"


def test_sanitizer_result_ok_empty():
    r = SanitizerResult(ok=True)
    assert r.findings == []
    assert r.scanners_run == []
    assert r.blocking() == []
    assert r.to_dict() == {"ok": True, "findings": [], "scanners_run": []}


def test_sanitizer_result_blocking_filter():
    blocking = Finding(kind=FindingKind.SECRET, severity=Severity.BLOCK, message="k")
    info = Finding(kind=FindingKind.PORTABILITY, severity=Severity.INFO, message="i")
    r = SanitizerResult(ok=False, findings=[blocking, info])
    assert r.blocking() == [blocking]


def test_review_result_defaults():
    r = ReviewResult(available=False)
    assert r.reviewer is None
    assert r.passed is False
    assert r.finding_count == 0
    d = r.to_dict()
    assert d["available"] is False
    assert d["reviewer"] is None


def test_version_gate_result_defaults():
    r = VersionGateResult(already_fixed=False, confidence=0)
    assert r.matched_sha is None
    assert r.parse_ok is True
    assert r.version_match is False
    assert r.llm_error is None


def test_divergence_result_clean():
    r = DivergenceResult(clean=True, message="ok")
    assert r.conflict_files == []
    assert r.to_dict()["clean"] is True


def test_install_info_to_dict():
    i = InstallInfo(install_id="abc", created_at="2026-04-11T00:00:00+00:00")
    d = i.to_dict()
    assert d["install_id"] == "abc"
    assert d["fingerprint_file"] is None
