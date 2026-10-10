"""Server-owned bounded input for peer sends; not a general HTTP timeout policy."""

from __future__ import annotations

import io
import time

from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, RequestTimeout
from werkzeug.serving import WSGIRequestHandler
from werkzeug.wrappers import Request

INPUT_KEY = "genesis.peer_request_input"
MAX_BODY_BYTES = 256 * 1024
BODY_TIMEOUT_S = 5.0
_CLEANUP_TIMEOUT_S = 0.25
_CLEANUP_BYTES = 64 * 1024
_MAX_WIRE_BYTES = MAX_BODY_BYTES * 16
_ROOT = "/v1/agent/a2a"


def _body_endpoint(environ):
    path = environ["PATH_INFO"]
    return environ["REQUEST_METHOD"] == "POST" and (
        path == _ROOT + "/message:send"
        or (path.startswith(_ROOT + "/tasks/") and path.endswith(":cancel"))
    )


class _DeadlineInput(io.RawIOBase):
    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self.deadline = None
        self.expired = False
        self.cleanup_remaining = None
        self.wire_remaining = None

    def readable(self):
        return True

    def cleanup(self):
        self.deadline = time.monotonic() + _CLEANUP_TIMEOUT_S
        self.cleanup_remaining = _CLEANUP_BYTES

    def readinto(self, buffer):
        if self.deadline is None:
            return self.connection.recv_into(buffer)
        remaining = self.deadline - time.monotonic()
        cleaning = self.cleanup_remaining is not None
        if remaining <= 0 or (cleaning and self.cleanup_remaining == 0):
            if cleaning:
                return 0
            self.expired = True
            raise TimeoutError("Request body deadline exceeded")
        if cleaning:
            buffer = memoryview(buffer)[: self.cleanup_remaining]
        elif self.wire_remaining is not None:
            # Werkzeug's chunk-header readline has no length limit. Bound
            # framing overhead as well as the decoded application body.
            buffer = memoryview(buffer)[: self.wire_remaining + 1]
        original = self.connection.gettimeout()
        try:
            self.connection.settimeout(remaining)
            count = self.connection.recv_into(buffer)
            if cleaning:
                self.cleanup_remaining -= count
            elif self.wire_remaining is not None:
                self.wire_remaining -= count
                if self.wire_remaining < 0:
                    raise RequestEntityTooLarge()
            return count
        except TimeoutError:
            if cleaning:
                return 0
            self.expired = True
            raise
        finally:
            self.connection.settimeout(original)


class PeerRequestInput:
    """Trusted capability installed by the host, never supplied by HTTP headers."""

    def __init__(self, raw, environ):
        self._raw = raw
        self._request = Request(environ)
        self._used = False

    def read(self):
        if self._used:
            raise BadRequest("Request body already consumed")
        self._used = True
        self._raw.deadline = time.monotonic() + BODY_TIMEOUT_S
        self._raw.wire_remaining = _MAX_WIRE_BYTES
        if (self._request.content_length or 0) > MAX_BODY_BYTES:
            raise RequestEntityTooLarge()
        buffer = bytearray(MAX_BODY_BYTES + 1)
        try:
            # A writable fixed-size view enforces readinto's buffer contract.
            # Incomplete chunk data must not resize the decoder's destination.
            count = self._request.stream.readinto(memoryview(buffer))
        except (BadRequest, OSError, ValueError):
            if self._raw.expired:
                raise RequestTimeout() from None
            raise BadRequest("Invalid request body") from None
        body = bytes(buffer[:count])
        if len(body) > MAX_BODY_BYTES:
            raise RequestEntityTooLarge()
        if self._request.content_length is not None and len(body) != self._request.content_length:
            raise BadRequest("Incomplete request body")
        return body


class PeerRequestHandler(WSGIRequestHandler):
    """Reuse Werkzeug framing; own per-receive deadlines below its buffering."""

    def setup(self):
        super().setup()
        self.rfile.close()
        self._peer_raw = _DeadlineInput(self.connection)
        self.rfile = io.BufferedReader(self._peer_raw)
        self._peer_input = None

    def make_environ(self):
        environ = super().make_environ()
        if _body_endpoint(environ):
            self._peer_input = PeerRequestInput(self._peer_raw, environ)
            environ[INPUT_KEY] = self._peer_input
        return environ

    def end_headers(self):
        # Responses that refuse a body must not strand a worker in Werkzeug's
        # unread-input drain. Bound both elapsed time and discarded bytes.
        if self._peer_input is not None:
            self._peer_raw.cleanup()
        super().end_headers()
