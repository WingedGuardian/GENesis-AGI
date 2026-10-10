"""Real sockets exercise deadline behavior beneath WSGI framing and cleanup."""

import contextlib
import hashlib
import json
import queue
import socket
import threading
import time

import pytest
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from genesis.hosting import request_input as bounded

PATH = "/v1/agent/a2a/message:send"


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(bounded, "BODY_TIMEOUT_S", 0.25)
    app = Flask(__name__)
    finished = queue.Queue()

    class Handler(bounded.PeerRequestHandler):
        def log_request(self, *args):
            pass

        def run_wsgi(self):
            try:
                super().run_wsgi()
            finally:
                finished.put(True)

    @app.route(PATH, methods=["POST"])
    @app.route("/v1/agent/a2a/tasks/fixture:cancel", methods=["POST"])
    @app.route("/ordinary", methods=["POST"])
    def consume():
        capability = request.environ.get(bounded.INPUT_KEY)
        refusal = request.headers.get("X-Fixture-Refusal")
        if refusal:
            return jsonify(error="refused"), int(refusal)
        if request.headers.get("X-Fixture-Auth-Delay"):
            time.sleep(0.35)
        try:
            body = capability.read() if capability else request.get_data()
        except HTTPException as error:
            return jsonify(error=error.name), error.code
        return jsonify(
            size=len(body), digest=hashlib.sha256(body).hexdigest(), bounded=bool(capability)
        )

    http = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Handler)
    thread = threading.Thread(target=http.serve_forever)
    thread.start()
    yield http.server_address, finished, app
    http.shutdown()
    http.server_close()
    thread.join(2)
    assert not thread.is_alive()


def exchange(server, payload, *, trickle=b"", delay=0.08):
    address, finished = server[:2]
    client = socket.create_connection(address, timeout=2)
    client.settimeout(2)
    client.sendall(payload)

    def send():
        for byte in trickle:
            try:
                client.sendall(bytes([byte]))
            except OSError:
                return
            time.sleep(delay)

    sender = threading.Thread(target=send)
    sender.start()
    started = time.monotonic()
    response = b""
    try:
        while True:
            data = client.recv(65536)
            if not data:
                break
            response += data
    finally:
        client.close()
        sender.join(2)
    assert not sender.is_alive()
    assert finished.get(timeout=1)
    return response, time.monotonic() - started


def headers(*, path=PATH, framing="Content-Length: 12", extra=""):
    return f"POST {path} HTTP/1.1\r\nHost: localhost\r\n{framing}\r\n{extra}\r\n".encode()


@pytest.mark.parametrize("size", [0, 1, bounded.MAX_BODY_BYTES, bounded.MAX_BODY_BYTES + 1])
def test_byte_boundary(server, size):
    body = b"a" * size
    response, _ = exchange(server, headers(framing=f"Content-Length: {size}") + body)
    expected = b"413" if size > bounded.MAX_BODY_BYTES else b"200"
    assert expected in response.split(b"\r\n", 1)[0]
    if expected == b"200":
        result = json.loads(response.split(b"\r\n\r\n", 1)[1])
        assert result["size"] == size and result["digest"] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize(
    "framing,initial",
    [
        ("Content-Length: 12", b""),
        ("Transfer-Encoding: chunked", b"c\r\n"),
        ("Transfer-Encoding: chunked", b""),
    ],
)
def test_trickle_has_absolute_deadline(server, framing, initial):
    response, elapsed = exchange(server, headers(framing=framing) + initial, trickle=b"a" * 20)
    assert b"408" in response.split(b"\r\n", 1)[0]
    assert elapsed < 0.9


@pytest.mark.parametrize("refusal", [401, 503, 413])
def test_refusal_cleanup_is_bounded(server, refusal):
    response, elapsed = exchange(
        server, headers(extra=f"X-Fixture-Refusal: {refusal}\r\n"), trickle=b"a" * 20
    )
    assert str(refusal).encode() in response.split(b"\r\n", 1)[0]
    assert elapsed < 0.9


@pytest.mark.parametrize(
    "body,status",
    [
        (b"3\r\nabc\r\n0\r\n\r\n", 200),
        (b"z\r\n", 400),
        (b"0\r\n\r\n", 200),
        (
            f"{bounded.MAX_BODY_BYTES + 1:x}\r\n".encode()
            + b"a" * (bounded.MAX_BODY_BYTES + 1)
            + b"\r\n0\r\n\r\n",
            413,
        ),
    ],
    ids=["valid", "invalid-header", "empty", "oversized"],
)
def test_chunked_framing_reuses_werkzeug(server, body, status):
    response, _ = exchange(server, headers(framing="Transfer-Encoding: chunked") + body)
    assert str(status).encode() in response.split(b"\r\n", 1)[0]


def test_unframed_body_is_not_read(server):
    response, _ = exchange(server, headers(framing=""))
    assert b"200" in response.split(b"\r\n", 1)[0]
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["size"] == 0


def test_ordinary_route_keeps_existing_read_behavior(server):
    response, elapsed = exchange(server, headers(path="/ordinary"), trickle=b"a" * 12)
    result = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert result["size"] == 12 and not result["bounded"]
    assert elapsed > 0.7


def test_cancel_route_has_capability(server):
    response, _ = exchange(
        server,
        headers(path="/v1/agent/a2a/tasks/fixture:cancel", framing="Content-Length: 2") + b"{}",
    )
    assert json.loads(response.split(b"\r\n\r\n", 1)[1])["bounded"]


def test_body_budget_begins_after_authentication(server):
    response, _ = exchange(
        server, headers(framing="Content-Length: 2", extra="X-Fixture-Auth-Delay: yes\r\n") + b"{}"
    )
    assert b"200" in response.split(b"\r\n", 1)[0]


def test_chunk_metadata_has_wire_byte_limit(server, monkeypatch):
    monkeypatch.setattr(bounded, "_MAX_WIRE_BYTES", 1024)
    response, _ = exchange(server, headers(framing="Transfer-Encoding: chunked") + b"a" * 16384)
    assert b"413" in response.split(b"\r\n", 1)[0]


def test_disconnected_partial_body_is_bad_request(server):
    address, finished = server[:2]
    with socket.create_connection(address, timeout=2) as client:
        client.sendall(headers() + b"a")
        client.shutdown(socket.SHUT_WR)
        response = client.recv(65536)
        assert b"400" in response.split(b"\r\n", 1)[0]
    assert finished.get(timeout=1)


@pytest.mark.parametrize("body", [b"3\r\na", b"3\r\nabc", b"3\r\nabc\r\n", b"0\r\n"])
def test_incomplete_chunked_body_is_bad_request(server, body):
    address, finished = server[:2]
    with socket.create_connection(address, timeout=2) as client:
        client.sendall(headers(framing="Transfer-Encoding: chunked") + body)
        client.shutdown(socket.SHUT_WR)
        response = client.recv(65536)
        assert b"400" in response.split(b"\r\n", 1)[0]
    assert finished.get(timeout=1)


def test_websocket_preserves_native_socket(server):
    # Add the actual existing Flask-Sock integration to the fixture app.
    from flask_sock import Sock
    from simple_websocket import Client, ConnectionClosed

    address, finished, app = server
    # Registration occurs before this fixture has served its first request.
    sock = Sock(app)

    @sock.route("/ws")
    def echo(ws):
        assert bounded.INPUT_KEY not in request.environ
        ws.send(ws.receive())

    class BoundedClient(Client):
        def handshake(self):
            self.sock.settimeout(2)
            try:
                super().handshake()
            finally:
                self.sock.settimeout(None)

    client = BoundedClient.connect(f"ws://{address[0]}:{address[1]}/ws")
    try:
        client.send("fixture")
        assert client.receive(timeout=2) == "fixture"
    finally:
        with contextlib.suppress(ConnectionClosed):
            client.close()
    assert finished.get(timeout=1)


def test_standalone_installs_handler_without_changing_listener():
    from unittest.mock import Mock

    from genesis.hosting.standalone import StandaloneAdapter

    adapter = StandaloneAdapter(host="127.0.0.1", port=5123)
    adapter._app = Mock()
    adapter._run_flask()
    adapter._app.run.assert_called_once_with(
        host="127.0.0.1",
        port=5123,
        threaded=True,
        use_reloader=False,
        request_handler=bounded.PeerRequestHandler,
    )
