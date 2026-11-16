"""Host-safe owned data probes; upstream execution belongs solely to contained CI."""

import importlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REF = "aaaaaaaa-1234-4567-8912-bbbbbbbbbbbb"
PATTERN = r"(?<edge>^|[^0-9])[0-9]{4}([ -][0-9]{4}){2,3}(?<end>[^0-9]|$)"


@pytest.fixture(scope="session")
def jq_required():
    # Probe the dependency, never GUARD/RENDER_RECORD: a broken program must fail.
    try:
        result = subprocess.run(["jq", "-n", "--argjson", "end", "1", "$end"],
                                capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        pytest.skip("jq is not installed")
    if result.returncode or result.stdout.strip() != "1":
        pytest.skip("jq lacks the required $end binding (jq 1.7 or newer)")


@pytest.mark.parametrize("availability", ["missing", "old", "wrong_output"])
def test_jq_dependency_probe_skips_only_unavailable_capability(monkeypatch, availability):
    def run(argv, **kwargs):
        assert argv == ["jq", "-n", "--argjson", "end", "1", "$end"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 5}
        if availability == "missing":
            raise FileNotFoundError("synthetic missing jq")
        return subprocess.CompletedProcess(argv, 3 if availability == "old" else 0,
                                           stdout="" if availability == "old" else "null\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(pytest.skip.Exception, match="jq"):
        jq_required.__wrapped__()


def test_jq_dependency_probe_accepts_the_required_feature(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert jq_required.__wrapped__() is None
    assert calls == [["jq", "-n", "--argjson", "end", "1", "$end"]]


def test_compatible_jq_does_not_hide_a_redaction_program_failure(modules, monkeypatch):
    def run(argv, **kwargs):
        feature_probe = argv[-1] == "$end"
        return subprocess.CompletedProcess(argv, 0 if feature_probe else 3,
                                           stdout="1\n" if feature_probe else "", stderr="synthetic filter failure")

    monkeypatch.setattr(subprocess, "run", run)
    assert jq_required.__wrapped__() is None
    with pytest.raises(AssertionError):
        test_unregistered_reference_is_not_exempt(modules)


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    return importlib.import_module("headlong_prompt_references"), importlib.import_module("headlong_reference_patch")


def guard(module, text, refs):
    return subprocess.run(["jq", "-Rsrj", "--argjson", "references", json.dumps(refs), "--arg", "pattern", PATTERN,
                           module.GUARD + "redact_card($pattern)"], input=text, capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("prefix", ["", "λ", "🙂λ", "e\u0301 "])
@pytest.mark.usefixtures("jq_required")
def test_registered_reference_and_ordinary_card(modules, prefix):
    _, patch = modules
    text = prefix + REF + " card 4111 1111 1111 1111 end"
    result = guard(patch, text, [REF])
    assert result.returncode == 0
    assert result.stdout == prefix + REF + " card <redacted:card> end"


@pytest.mark.usefixtures("jq_required")
def test_unregistered_reference_is_not_exempt(modules):
    _, patch = modules
    result = guard(patch, REF, [])
    assert result.returncode == 0
    assert result.stdout == "aaaaaaaa-<redacted:card>-bbbbbbbbbbbb"


@pytest.mark.usefixtures("jq_required")
def test_crossing_reference_is_typed_conflict_without_input_leak(modules):
    _, patch = modules
    ref = "aaaaaaaa-1234-4567-8912-bbbbbbbb4111"
    result = guard(patch, ref + " 1111 1111", [ref])
    assert result.returncode == 65
    assert result.stdout == ""
    assert result.stderr == "reference conflict\n"


@pytest.mark.parametrize("letter,escaped", [("a", r"\u0061"), ("b", r"\u0062"), ("1", r"\u0031"), ("-", r"\u002d")])
@pytest.mark.parametrize("as_key", [False, True])
@pytest.mark.usefixtures("jq_required")
def test_encoded_reference_is_literal_before_final_pass(modules, letter, escaped, as_key):
    _, patch = modules
    record = {REF: "🙂λ " + REF} if as_key else {"details": "🙂λ " + REF}
    text = json.dumps(record, ensure_ascii=False).replace(letter, escaped)
    program = (patch.GUARD + '. as $line | fromjson as $record | $record as $clean | '
               + patch.RENDER_RECORD + ' | redact_card($pattern)')
    result = subprocess.run(["jq", "-Rsrj", "--argjson", "references", json.dumps([REF]), "--arg", "pattern", PATTERN,
                             program], input=text, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert REF in result.stdout
    assert json.loads(result.stdout) == record


@pytest.fixture
def owned_config(tmp_path):
    root, run = tmp_path / "case", tmp_path / "case/run-0"
    run.mkdir(parents=True)
    config = {"root": str(root), "run_root": str(run), "identity_dir": str(root / "identity"),
              "traj_id": "fixture-trajectory", "trigger_id": REF, "invocation_id": "a" * 32, "reference_ids": [REF]}
    path = run / "config.json"
    path.write_text(json.dumps(config))
    env = {"CONTROL_CONFIG": str(path), "HEADLONG_REFERENCE_ROOT": str(root), "HEADLONG_REFERENCE_RUN_ROOT": str(run),
           "HEADLONG_REFERENCE_NONCE": "a" * 32, "HEADLONG_REFERENCE_TRIGGER": REF,
           "IDENTITY_DIR": str(root / "identity"), "TRAJ_ID": "fixture-trajectory"}
    return path, config, env, tmp_path


def test_config_complete_and_explicit_empty_list(modules, owned_config):
    refs, _ = modules
    path, config, env, base = owned_config
    assert refs.load_config(env, base)["reference_ids"] == [REF]
    config["reference_ids"] = []
    path.write_text(json.dumps(config))
    assert refs.load_config(env, base)["reference_ids"] == []


def test_publisher_never_reuses_an_invocation(modules, owned_config):
    refs, _ = modules
    _, config, env, _ = owned_config
    with pytest.raises(ValueError, match="reused"):
        refs.publish_config(config, env)


def test_pending_publication_cannot_be_read(modules, owned_config):
    refs, _ = modules
    path, _, env, base = owned_config
    (path.parent / "config.pending").write_text("partial")
    with pytest.raises(ValueError, match="pending"):
        refs.load_config(env, base)


@pytest.mark.parametrize("defect", ["missing", "partial", "stale", "wrong_run", "wrong_root", "wrong_identity", "wrong_traj",
                                     "wrong_trigger", "no_refs", "bad_ref", "duplicate_ref", "bad_nonce"])
def test_config_lifecycle_rejects_incomplete_or_misbound_state(modules, owned_config, defect):
    refs, _ = modules
    path, config, env, base = owned_config
    if defect == "missing":
        path.unlink()
    elif defect == "partial":
        path.write_text('{"root":')
    else:
        key, value = {"stale": ("invocation_id", "b" * 32), "wrong_run": ("run_root", str(base / "other")),
                      "wrong_root": ("root", str(base / "other")), "wrong_identity": ("identity_dir", str(base / "other")),
                      "wrong_traj": ("traj_id", "other"), "wrong_trigger": ("trigger_id", "other"),
                      "no_refs": ("reference_ids", None), "bad_ref": ("reference_ids", ["card 4111 1111 1111 1111"]),
                      "duplicate_ref": ("reference_ids", [REF, REF]), "bad_nonce": ("invocation_id", "")}[defect]
        config[key] = value
        path.write_text(json.dumps(config))
    with pytest.raises((ValueError, OSError, KeyError)):
        refs.load_config(env, base)
