"""External evidence refuses unsafe inputs durably without resending completions."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from threading import Thread

import httpx
import pytest

from genesis.eval.qualification import pinned
from tests.test_eval.qualification_transport_fixtures import Gateway, ask, bundle, router


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(pinned.KEY_ENV, "synthetic-only-key")
    secret_file = tmp_path / "secrets.env"
    secret_file.write_text("")
    monkeypatch.setenv("SECRETS_PATH", str(secret_file))
    for name in pinned.PRODUCTION_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("malformed", ["escape", "unterminated", "unquoted"])
@pytest.mark.parametrize("sink", ["body", "exception", "log"])
def test_opaque_escaped_credential_never_survives(env, malformed, sink):
    encoded = "".join("\\u" + format(ord(c), "04x") for c in "synthetic-only-key")
    text = '{"error":"' + encoded + ('\\q"}' if malformed == "escape" else "")
    if malformed == "unquoted":
        text = "upstream: " + encoded
    if sink == "body":
        result = pinned.safe_body(text.encode())
    elif sink == "exception":
        result = pinned.failure_message(ValueError(text))
    else:
        original = logging.getLogRecordFactory()
        with pinned.private_logs():
            record = logging.getLogRecordFactory()("synthetic", 40, __file__, 1, text, (), None)
            result = logging.Formatter().format(record)
        assert logging.getLogRecordFactory() is original
    assert encoded not in result and "synthetic-only-key" not in result
    assert "[REDACTED" in result


@pytest.mark.parametrize("depth,accepted", [(63, True), (64, True), (65, False), (1200, False)])
def test_external_nesting_bound_ignores_brackets_inside_strings(depth, accepted):
    raw = "[" * depth + '"[\\\"}[]"' + "]" * depth
    if accepted:
        assert pinned.external_json(raw) is not None
    else:
        with pytest.raises(pinned.LocalFailure, match="external JSON"):
            pinned.external_json(raw)
        assert pinned.safe_body(raw.encode()) == pinned.OPAQUE_EVIDENCE


@pytest.mark.parametrize("unicode", [False, True])
def test_body_byte_bound_exact_edge_and_overflow(unicode):
    value = "é" if unicode else "x"
    count = (pinned.MAX_EVIDENCE_BYTES - 2) // len(value.encode())
    raw = ('"' + value * count + '"').encode()
    assert len(raw) == pinned.MAX_EVIDENCE_BYTES
    assert pinned.external_json(raw) == value * count
    with pytest.raises(pinned.LocalFailure):
        pinned.external_json(raw + b" ")
    assert pinned.safe_body(raw + b" ") == pinned.OPAQUE_EVIDENCE


@pytest.mark.parametrize("where", ["usage", "association", "response_headers", "other"])
def test_structured_normalization_refuses_before_python_recursion(where):
    value = 0
    for _ in range(300):
        value = {"nested": value}
    with pytest.raises(pinned.LocalFailure, match="nesting"):
        pinned.safe_evidence({where: value})


@pytest.mark.parametrize("failure", ["deep", "large", "malformed"])
async def test_post_refusal_is_durable_funded_and_never_resent(env, failure):
    data, gateway = bundle(env), Gateway()

    def transport(request):
        response = gateway(request)
        if request.method != "POST":
            return response
        if failure == "malformed":
            raw = b'{"error":"\\u0073\\q"}'
        elif failure == "large":
            raw = ('{"extra":"' + "x" * pinned.MAX_EVIDENCE_BYTES + '"}').encode()
        else:
            raw = response.content[:-1] + b',"extra":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"
        return httpx.Response(200, content=raw)

    with data.campaign:
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway, transport=httpx.MockTransport(transport)), data.messages)
        row = data.campaign.attempt("case-1")
        assert len(row["observations"]) == 1 and row["observations"][0]["error"]
        assert row["observations"][0]["raw_body"] == pinned.OPAQUE_EVIDENCE
        assert data.campaign.state.reserved == Decimal("0.1")
    with data.campaign:
        assert data.campaign.state.blockers and data.campaign.state.reserved == Decimal("0.1")
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway), data.messages)
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("failure", ["deep", "large", "malformed"])
async def test_get_refusal_retains_invalid_receipt_on_reopen(env, failure):
    data, gateway = bundle(env), Gateway()
    with data.campaign:
        await ask(router(data, gateway), data.messages)
        previous = data.campaign.attempt("case-1")["receipts"]

        def transport(request):
            assert request.method == "GET" and request.url.path.endswith("/generation")
            raw = b'{"data":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"
            if failure == "large":
                raw = b" " * (pinned.MAX_EVIDENCE_BYTES + 1)
            if failure == "malformed":
                raw = b'{"error":"\\u0073\\q"}'
            return httpx.Response(200, content=raw)

        with pytest.raises(pinned.LocalFailure):
            await pinned.reconcile(data.campaign, "case-1", transport=httpx.MockTransport(transport))
        receipts = data.campaign.attempt("case-1")["receipts"]
        assert receipts[:-1] == previous and len(receipts) == len(previous) + 1
        assert receipts[-1]["error"]
        assert receipts[-1]["raw_body"] == pinned.OPAQUE_EVIDENCE and "charge" not in receipts[-1]
    with data.campaign:
        assert data.campaign.state.blockers and data.campaign.state.reserved == Decimal("0.1")
        with pytest.raises(pinned.LocalFailure):
            await ask(router(data, gateway), data.messages)
    assert len(gateway.requests) == 1


def test_valid_decimal_and_schema_collision_controls(env, monkeypatch):
    monkeypatch.setenv(pinned.KEY_ENV, "usage")
    raw = b'{"usage":{"cost":0.123456789012345678901},"content":"usage"}'
    safe = pinned.safe_body(raw)
    assert json.loads(safe)["content"] == "[REDACTED]"
    assert "0.123456789012345678901" in safe
    assert pinned.financial_json(raw, (("usage", "cost"),))["usage"]["cost"] == "0.123456789012345678901"


@pytest.mark.parametrize("spelling", [r"\ud800", r"\udc00", r"\ud83d\ude00"])
def test_json_surrogates_keep_existing_canonical_serialization(spelling):
    from genesis.eval.qualification.evidence import canonical

    raw = ('{"content":"' + spelling + '"}').encode()
    retained = json.loads(pinned.safe_body(raw))
    assert retained == json.loads(raw)
    assert json.loads(canonical(retained)) == retained


def test_decoder_recursion_is_a_normalized_refusal(monkeypatch):
    def fault(_raw):
        raise RecursionError("synthetic decoder fault")

    monkeypatch.setattr(pinned, "load_json", fault)
    with pytest.raises(pinned.LocalFailure, match="external JSON"):
        pinned.external_json(b"{}")
    assert pinned.safe_body(b"{}") == pinned.OPAQUE_EVIDENCE


@pytest.mark.parametrize("layers", [1, 2, 5])
@pytest.mark.parametrize("surface", ["wire", "diagnostic", "value"])
def test_decoded_json_layers_still_pass_the_text_boundary(env, layers, surface):
    secret = "synthetic-only-key"
    value = "".join("\\u" + format(ord(c), "04x") for c in secret)
    for _ in range(layers):
        value = json.dumps({"error": value})
    raw = json.dumps({"content": value, "usage": {"cost": 0.0001}})
    if surface == "wire":
        result = pinned.safe_body(raw.encode())
        assert json.loads(result)["usage"]["cost"] == 0.0001
    elif surface == "diagnostic":
        result = pinned.safe_text("upstream: " + raw)
    else:
        result = pinned.safe_evidence({"content": value})["content"]
    assert "[REDACTED" in result and secret not in result
    assert "0073" not in result


def test_nested_numeric_text_does_not_gain_wire_money_trust(env, monkeypatch):
    monkeypatch.setenv(pinned.KEY_ENV, "12345678")
    raw = json.dumps({"content": '{"number":12345678}', "usage": {"cost": 0.12345678}})
    retained = json.loads(pinned.safe_body(raw.encode()))
    assert "12345678" not in retained["content"]
    assert retained["usage"]["cost"] == 0.12345678


@pytest.mark.parametrize("alias", pinned.PRODUCTION_KEY_NAMES)
@pytest.mark.parametrize("logs_first", [False, True])
@pytest.mark.parametrize("exit_kind", ["normal", "error", "cancel"])
def test_hidden_credentials_stay_redacted_on_logging_threads(env, monkeypatch, alias, logs_first, exit_kind):
    production = "synthetic-production-secret"
    monkeypatch.setenv(alias, production)
    before = {name: os.environ.get(name) for name in pinned.PRODUCTION_KEY_NAMES}
    factory = logging.getLogRecordFactory()
    seen = []

    def log_from_thread():
        record = logging.getLogRecordFactory()("synthetic", 40, __file__, 1, "%s", (production,), None)
        seen.append(logging.Formatter().format(record))

    try:
        with ExitStack() as stack:
            contexts = [pinned.private_logs(), pinned.scoped_credential("synthetic-only-key")]
            for context in contexts if logs_first else reversed(contexts):
                stack.enter_context(context)
            assert os.environ["OPENROUTER_API_KEY"] == "synthetic-only-key"
            assert all(name not in os.environ for name in pinned.PRODUCTION_KEY_NAMES if name != "OPENROUTER_API_KEY")
            assert pinned.safe_text(production) == "[REDACTED]"
            thread = Thread(target=log_from_thread)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive() and seen == ["[REDACTED]"]
            if exit_kind == "error":
                raise ValueError("synthetic")
            if exit_kind == "cancel":
                raise asyncio.CancelledError()
    except (ValueError, asyncio.CancelledError):
        assert exit_kind != "normal"
    assert {name: os.environ.get(name) for name in pinned.PRODUCTION_KEY_NAMES} == before
    assert logging.getLogRecordFactory() is factory
    assert not pinned._SCOPED_SECRETS.get()


def test_nested_credentials_and_failed_setup_restore_context(env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_TOKEN", "synthetic-production-secret")
    before = {name: os.environ.get(name) for name in pinned.PRODUCTION_KEY_NAMES}
    with pinned.scoped_credential("synthetic-outer-key"):
        with pinned.scoped_credential("synthetic-inner-key"):
            for value in ("synthetic-production-secret", "synthetic-outer-key", "synthetic-inner-key"):
                assert pinned.safe_text(value) == "[REDACTED]"
        assert os.environ["OPENROUTER_API_KEY"] == "synthetic-outer-key"
        assert "synthetic-inner-key" not in pinned._redaction_values()
        with pytest.raises(TypeError), pinned.scoped_credential(None):
            pytest.fail("invalid credential accepted")
        assert os.environ["OPENROUTER_API_KEY"] == "synthetic-outer-key"
    assert {name: os.environ.get(name) for name in pinned.PRODUCTION_KEY_NAMES} == before
    assert not pinned._SCOPED_SECRETS.get()


@pytest.mark.parametrize("alias", pinned.PRODUCTION_KEY_NAMES)
async def test_production_echo_is_redacted_through_delegate_journal_and_reopen(env, monkeypatch, alias):
    production = "synthetic-production-secret"
    monkeypatch.setenv(alias, production)
    data, gateway = bundle(env), Gateway(answer=production)

    def transport(request):
        if request.method == "POST":
            assert request.headers["authorization"] == "Bearer synthetic-only-key"
            assert pinned.safe_text(production) == "[REDACTED]"
        return gateway(request)

    with data.campaign:
        await ask(router(data, gateway, transport=httpx.MockTransport(transport)), data.messages)
        assert data.campaign.attempt("case-1")["observations"][0]["content"] == "[REDACTED]"
    for path in (env / "delegate-campaign").rglob("*.json*"):
        assert production.encode() not in path.read_bytes()
    with data.campaign:
        assert data.campaign.attempt("case-1")["observations"][0]["content"] == "[REDACTED]"
        replay = await ask(router(data, gateway), data.messages)
        assert replay.content == "[REDACTED]"
    assert len(gateway.requests) == 1 and os.environ[alias] == production


def test_malformed_quoted_input_finishes_before_cpu_budget():
    # Extract actual helpers to exclude SDK startup from the runtime bound.
    # The previous regex exhausts this CPU budget on the same 32,001-byte input.
    script = r'''
import ast, json, os, re, resource, sys
from contextvars import ContextVar
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
root = Path(sys.argv[1])
ns = dict(json=json, os=os, re=re, ContextVar=ContextVar)
functions = {"load_json", "_literal_text", "_redaction_values", "_json_document", "_json_parts", "_redacted_text",
             "external_json", "safe_text", "safe_body"}
constants = {"KEY_ENV", "MAX_EVIDENCE_BYTES", "MAX_EVIDENCE_DEPTH",
             "OPAQUE_EVIDENCE", "_JSON_PARTS", "_WIRE_FIELDS", "_KEY_COLON", "_SCOPED_SECRETS"}
for relative in ("evidence.py", "pinned.py"):
    path = root / "src/genesis/eval/qualification" / relative
    nodes = [n for n in ast.parse(path.read_text()).body
             if (isinstance(n, ast.FunctionDef) and n.name in functions)
             or (isinstance(n, ast.ClassDef) and n.name in {"Incomplete", "LocalFailure"})
             or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name)
                 and t.id in constants for t in n.targets))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)
raw = chr(34) + (chr(92) + chr(34)) * 16000
assert "[REDACTED" in ns["safe_text"](raw)
assert "[REDACTED" in ns["safe_body"](raw.encode())
print("PASS")
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[2])],
        capture_output=True, text=True, timeout=6,
    )
    assert result.returncode == 0 and result.stdout.strip() == "PASS", result.stderr
