"""HTTP server for the Tier-2 Python runtime (Phase 11 + 12).

stdlib http.server only (dependency-free). Binds loopback and dispatches every
request through the Router, serializing handler payloads as JSON. The bound port
is a Python-service port (47337+), never a VS Code window-band port — the runtime
config guard enforces that upstream.
"""

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _NoBody:
    """Sentinel: emit an EMPTY response body (distinct from None -> JSON `null`).

    Adapter-backed routes use this for a JS `undefined` result (/exec), which the
    live runtime serializes as a 200 with Content-Length 0 (see WIRE_CONTRACT.md
    and gate phase12a1-exec-undef-011).
    """

    __slots__ = ()

    def __repr__(self):
        return "NO_BODY"


NO_BODY = _NoBody()


class _Handler(BaseHTTPRequestHandler):
    server_version = "codeapi-runtime/0"

    def _handle(self, method):
        # Opt-in public-service authentication, distinct from the mandatory
        # private bridge token. Keep health/session available for discovery.
        token = getattr(self.server, "public_token", "")
        discovery = method == "GET" and self.path.split("?", 1)[0] in ("/health", "/session")
        if token and not discovery:
            supplied = self.headers.get("X-CODEAPI-TOKEN", "")
            if not secrets.compare_digest(supplied, token):
                data = b'{"error":"CODEAPI authentication required"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            status, payload = self.server.router.dispatch(method, self.path, body)
        except Exception as e:  # noqa: BLE001 - never leak a stack to the wire
            status, payload = 500, {"error": f"{type(e).__name__}: {e}"}
        # NO_BODY -> empty body (e.g. /exec of a JS `undefined`, Content-Length 0).
        # Everything else, INCLUDING None, is JSON-serialized so None -> "null".
        # Compact separators match the extension's res.json() byte-for-byte;
        # insertion order preserved, keys never sorted.
        if payload is NO_BODY:
            data = b""
        else:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and method != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        # Required for DELETE /term/:id (Phase 13D). Same dispatch path as GET/POST.
        self._handle("DELETE")

    def log_message(self, *args):
        # Quiet by default; the runtime logs lifecycle to stdout/stderr, which the
        # JS adapter pipes into the CODEAPI output channel.
        pass


class HttpServer:
    """Loopback HTTP server hosting the CODEAPI router."""

    def __init__(self, host="127.0.0.1", port=None, router=None, public_token=None):
        self.host = host
        self.port = port
        self.router = router
        self.public_token = (os.environ.get("CODEAPI_PUBLIC_TOKEN", "")
                             if public_token is None else public_token)
        self._httpd = None

    def start(self, poll_interval=0.5):
        """Bind and serve forever (blocks). Loopback only."""
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.router = self.router
        self._httpd.public_token = self.public_token
        self._httpd.serve_forever(poll_interval=poll_interval)

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
