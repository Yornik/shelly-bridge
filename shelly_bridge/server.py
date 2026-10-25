"""The bridge's own HTTP endpoints: /metrics, /healthz and /readyz."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import REGISTRY, make_wsgi_app
from prometheus_client.registry import CollectorRegistry


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        """Every scrape and probe would otherwise be a log line."""


def _text(start_response, status: str, body: str, headers=()) -> Iterable[bytes]:
    data = body.encode()
    start_response(
        status,
        [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(data))),
            *headers,
        ],
    )
    return [data]


def make_app(is_ready: Callable[[], bool], registry: CollectorRegistry = REGISTRY):
    metrics_app = make_wsgi_app(registry)

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "")
        if method not in ("GET", "HEAD"):
            return _text(
                start_response, "405 Method Not Allowed", "only GET\n", [("Allow", "GET, HEAD")]
            )
        if path == "/metrics":
            return metrics_app(environ, start_response)
        if path == "/healthz":
            # The process answers, so it is alive. Whether the device does is /readyz.
            return _text(start_response, "200 OK", "ok\n")
        if path == "/readyz":
            if is_ready():
                return _text(start_response, "200 OK", "ready\n")
            return _text(
                start_response,
                "503 Service Unavailable",
                "not ready: no successful poll of the device in the last few intervals\n",
            )
        return _text(start_response, "404 Not Found", "not found; try /metrics\n")

    return app


def start_server(
    port: int,
    is_ready: Callable[[], bool],
    *,
    addr: str = "0.0.0.0",  # noqa: S104 - Prometheus scrapes the pod from outside
    registry: CollectorRegistry = REGISTRY,
) -> WSGIServer:
    server = make_server(addr, port, make_app(is_ready, registry), _Server, _QuietHandler)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    return server
