"""Two independent WebSocket clients of one explicitly owned native app-server."""

import json
import os
import queue
import select
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from threading import Event, Thread

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect

from tests.test_hooks.native_codex import AppServer


class Connection(AppServer):
    """Reuse the existing RPC protocol driver with a different wire transport."""

    def __init__(self, socket_path):
        self.connection = unix_connect(str(socket_path), open_timeout=5)
        self.events = []
        self.pending = queue.Queue()
        self.closing = Event()
        self.reader_error = None
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for message in self.connection:
                self.pending.put(json.loads(message))
        except ConnectionClosed as exc:
            if not self.closing.is_set():
                self.reader_error = str(exc)
        except (ValueError, TypeError) as exc:
            self.reader_error = str(exc)
        finally:
            if not self.closing.is_set() and self.reader_error is None:
                self.reader_error = "server closed the connection unexpectedly"
            self.pending.put({"closed": True, "reader_error": self.reader_error})

    def send(self, method, params, request_id=None):
        message = {"method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.connection.send(json.dumps(message))

    def close(self):
        self.closing.set()
        self.connection.close()
        self.reader.join(timeout=5)
        assert not self.reader.is_alive(), "WebSocket reader did not stop"
        assert self.reader_error is None, self.reader_error


class ConnectedServer:
    def __init__(self, binary, root, env):
        # Short private path respects AF_UNIX's path limit even when the test's
        # workspace path is long. No live/default Codex control socket is used.
        scratch = Path.home() / "tmp"
        scratch.mkdir(exist_ok=True)
        self.cleanup = ExitStack()
        directory = self.cleanup.enter_context(tempfile.TemporaryDirectory(prefix="mcp-socket-", dir=scratch))
        self.socket_path = Path(directory) / "control.sock"
        self.clients = []
        self.log = self.cleanup.enter_context((root / "app-stderr.txt").open("a"))
        self.process = None
        try:
            self.process = subprocess.Popen(
                [binary, "app-server", "--listen", f"unix://{self.socket_path}"],
                cwd=root / "workspace", env=env, stdout=self.log, stderr=self.log,
                start_new_session=True,
            )
            self.cleanup.callback(self._stop_process)
            self.pidfd = os.pidfd_open(self.process.pid)
            self.cleanup.callback(os.close, self.pidfd)
            deadline = time.monotonic() + 30
            while not self.socket_path.exists():
                # poll()/wait() would release the leader PID before group
                # cleanup, permitting accidental signals after PGID reuse.
                assert not select.select([self.pidfd], [], [], 0)[0], "native server exited before listening"
                assert time.monotonic() < deadline, "native server did not create fixture socket"
                time.sleep(0.01)
        except BaseException:
            self.close()
            raise

    def connect(self):
        client = Connection(self.socket_path)
        self.cleanup.callback(client.close)
        self.clients.append(client)
        client.initialize()
        return client

    def close(self):
        self.cleanup.close()

    def _stop_process(self):
        if self.process is not None:
            # This instance starts its own process group. No process discovery
            # or live-daemon lookup is used to select cleanup targets.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
