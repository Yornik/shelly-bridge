"""A fake Shelly Gen2 device on 127.0.0.1, for testing the real client over real HTTP.

It answers GET /rpc/<Method> like firmware 1.7.x: Shelly.GetDeviceInfo without
authentication, everything else behind SHA-256 digest auth when a password is
set, with numeric nonces that (by default) are good for one request only. It
records every request, including the ones a read-only bridge must never send.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class FakeShelly:
    def __init__(self, info: dict, status: dict, switch_config: dict, password: str | None = None):
        self.info = copy.deepcopy(info)
        self.status = copy.deepcopy(status)
        self.switch_config = copy.deepcopy(switch_config)
        self.password = password
        self.realm = self.info["id"]
        # Knobs a test turns to break the device in one particular way.
        self.delay = 0.0
        self.raw_body: bytes | None = None
        self.http_status: int | None = None
        self.redirect_to: str | None = None
        self.one_use_nonces = True
        # Send a 200 body one byte at a time, this many seconds apart.
        self.trickle = 0.0
        # Send only this many bytes of a 200 body, wait `stall` seconds, then hang up.
        self.cut_body: int | None = None
        self.stall = 0.0
        # (HTTP method, path with query, whether auth was accepted)
        self.requests: list[tuple[str, str, bool]] = []
        self._nonces: set[str] = set()
        self._next_nonce = int(time.time())
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                fake._handle(self)

            def _refuse(self):
                fake.requests.append((self.command, self.path, False))
                fake._send(self, 405, b'{"code":405,"message":"only GET here"}')

            do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _refuse

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a client that gave up (timeout test) closes the socket mid-reply

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    # -- protocol ----------------------------------------------------------

    def _send(self, handler, code: int, body: bytes, headers=()) -> None:
        handler.send_response(code)
        for name, value in headers:
            handler.send_header(name, value)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        # Like the real device: one request per connection.
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.close_connection = True
        if code == 200 and self.cut_body is not None:
            handler.wfile.write(body[: self.cut_body])
            time.sleep(self.stall)
        elif code == 200 and self.trickle:
            for i in range(len(body)):
                handler.wfile.write(body[i : i + 1])
                time.sleep(self.trickle)
        else:
            handler.wfile.write(body)

    def _challenge(self, handler) -> None:
        with self._lock:
            self._next_nonce += 1
            nonce = str(self._next_nonce)
            self._nonces.add(nonce)
        header = f'Digest qop="auth", realm="{self.realm}", nonce="{nonce}", algorithm=SHA-256'
        self._send(handler, 401, b"", [("WWW-Authenticate", header)])

    def _authorized(self, handler) -> bool:
        value = handler.headers.get("Authorization", "")
        if not value.startswith("Digest "):
            return False
        fields = {k: a or b for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', value[7:])}
        nonce = fields.get("nonce", "")
        with self._lock:
            if nonce not in self._nonces:
                return False
            if self.one_use_nonces:
                self._nonces.discard(nonce)
        ha1 = _sha256(f"admin:{self.realm}:{self.password}")
        ha2 = _sha256(f"{handler.command}:{fields.get('uri')}")
        expected = _sha256(f"{ha1}:{nonce}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}")
        return (
            fields.get("username") == "admin"
            and fields.get("realm") == self.realm
            and fields.get("algorithm", "").upper() == "SHA-256"
            and fields.get("uri") == handler.path
            and fields.get("response") == expected
        )

    def _handle(self, handler) -> None:
        if self.delay:
            time.sleep(self.delay)
        url = urlsplit(handler.path)
        method = url.path.removeprefix("/rpc/") if url.path.startswith("/rpc/") else None
        public = method is not None and method.casefold() == "shelly.getdeviceinfo"
        if self.password and not public and not self._authorized(handler):
            self.requests.append((handler.command, handler.path, False))
            self._challenge(handler)
            return
        self.requests.append((handler.command, handler.path, True))
        if self.redirect_to is not None:
            self._send(handler, 302, b"", [("Location", self.redirect_to)])
            return
        if self.http_status is not None:
            self._send(handler, self.http_status, b'{"code":-114,"message":"unavailable"}')
            return
        if self.raw_body is not None:
            self._send(handler, 200, self.raw_body)
            return
        result = self._result(method, parse_qs(url.query))
        if result is None:
            body = json.dumps({"code": 404, "message": f"No handler for {method}"})
            self._send(handler, 404, body.encode())
            return
        self._send(handler, 200, json.dumps(result).encode())

    def _result(self, method: str | None, query: dict) -> dict | None:
        folded = (method or "").casefold()
        if folded == "shelly.getdeviceinfo":
            return self.info
        if folded == "shelly.getstatus":
            return self.status
        if folded == "switch.getconfig" and query.get("id") == ["0"]:
            return self.switch_config
        return None
