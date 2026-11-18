"""Host-side, task-scoped model-API relay.

Agent containers have no network. For local models the orchestrator bind-mounts a directory containing a
unix socket served by this relay; inside the container a tiny TCP->socket forwarder (agent/pi/llm-relay.js)
exposes it as 127.0.0.1:11434. The relay speaks HTTP and forwards ONLY an allow-list of routes, and only for
the task's configured model(s), to a fixed upstream (the local Ollama). Everything else (model pull/delete,
other models, arbitrary hosts) is refused. It also records upstream errors so the orchestrator can tell
model/auth/infrastructure failures apart.
"""
from __future__ import annotations

import http.client
import json
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

DEFAULT_UPSTREAM = ("127.0.0.1", 11434)
SOCKET_NAME = "llm.sock"
MAX_BODY = 8 * 1024 * 1024
_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
        "proxy-authenticate", "content-length", "host"}
ALLOWED_GET = ("/v1/models",)
ALLOWED_POST = ("/v1/chat/completions",)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # client hang-ups are normal on cancel/timeout
        pass


class LLMRelay:
    def __init__(self, socket_dir, allowed_models, upstream=DEFAULT_UPSTREAM, upstream_timeout=900):
        self.socket_dir = Path(socket_dir)
        self.path = self.socket_dir / SOCKET_NAME
        self.allowed_models = set(allowed_models)
        self.upstream = upstream
        self.upstream_timeout = upstream_timeout
        self.lock = threading.Lock()
        self.requests = 0
        self.denied: list = []
        self.upstream_errors: list = []  # (status, text) of upstream HTTP >= 400
        self.unreachable: list = []
        self._server = None
        self._thread = None

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> "LLMRelay":
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_dir, 0o755)
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()  # stale socket from a crashed orchestrator
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def address_string(self):
                return "container"

            def do_GET(self):
                relay._handle(self, "GET")

            def do_POST(self):
                relay._handle(self, "POST")

            def do_PUT(self):
                relay._deny(self, 405, "method not allowed")

            do_DELETE = do_PATCH = do_PUT

        self._server = _Server(str(self.path), Handler)
        os.chmod(self.path, 0o666)  # container uid differs from the orchestrator's owner bits only via bind mount
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="llm-relay")
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # -- request handling ----------------------------------------------------------
    def _deny(self, h, status, reason):
        with self.lock:
            self.denied.append(f"{h.command} {h.path}: {reason}")
            del self.denied[:-50]
        body = json.dumps({"error": {"message": f"relay: {reason}", "type": "relay_denied"}}).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle(self, h, method):
        path = h.path.split("?", 1)[0].rstrip("/")
        body = b""
        if method == "GET":
            if not (path in ALLOWED_GET or path.startswith(ALLOWED_GET[0] + "/")):
                return self._deny(h, 403, "route not allowed")
        else:
            if path not in ALLOWED_POST:
                return self._deny(h, 403, "route not allowed")
            if h.headers.get("Transfer-Encoding", "").lower() == "chunked":
                return self._deny(h, 411, "chunked request bodies not supported")
            length = int(h.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._deny(h, 413, "request too large")
            body = h.rfile.read(length)
            try:
                model = json.loads(body).get("model")
            except (ValueError, AttributeError):
                return self._deny(h, 400, "invalid JSON body")
            if model not in self.allowed_models:
                return self._deny(h, 403, f"model {model!r} not allowed for this task")
        with self.lock:
            self.requests += 1
        try:
            conn = http.client.HTTPConnection(*self.upstream, timeout=self.upstream_timeout)
            headers = {k: v for k, v in h.headers.items() if k.lower() in ("content-type", "accept")}
            conn.request(method, h.path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            with self.lock:
                self.unreachable.append(str(exc))
            return self._deny(h, 502, f"upstream unreachable: {exc}")
        try:
            h.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in _HOP:
                    h.send_header(k, v)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            first = True
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if first and resp.status >= 400:
                    with self.lock:
                        self.upstream_errors.append((resp.status, chunk[:300].decode(errors="replace")))
                        del self.upstream_errors[:-20]
                first = False
                h.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                h.wfile.flush()
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            h.close_connection = True
        finally:
            conn.close()
