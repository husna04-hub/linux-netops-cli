"""Shared fixtures: tiny controllable HTTP servers for integration tests."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

import pytest


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        behavior: Callable[[_Handler], None] = self.server.behavior  # type: ignore[attr-defined]
        behavior(self)

    def log_message(self, *args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, behavior: Callable[[_Handler], None]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.behavior = behavior


@pytest.fixture
def start_http_server():
    servers: list[tuple[_Server, threading.Thread]] = []

    def start(behavior: Callable[[_Handler], None]) -> tuple[_Server, str]:
        server = _Server(behavior)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server, f"http://127.0.0.1:{server.server_port}"

    yield start

    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def respond(
    handler: _Handler,
    status: int = 200,
    body: bytes = b'{"ok": true}',
    headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)
