#!/usr/bin/env python3
"""Phase 12E acceptance — public adapter-backed route shaping, WITHOUT VS Code.

Runs the REAL Tier-2 server with a FAKE adapter injected, and checks the PUBLIC
wire shapes at the byte level: /exec (unwrapped / null / empty-on-undefined),
/command (wrapped, result omitted when undefined), showMessage -> {success:true},
workspace/editor/apis passthrough, 500-on-error, 503-on-unavailable, and /term/*
404. Complements test_runtime_pure (503-when-unconfigured) and test_adapter_client
(the client itself).

    python python/tests/test_vscode_routes.py
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.runtime import create_server                       # noqa: E402
from codeapi_vscode.session.port_allocator import PortAllocator        # noqa: E402
from codeapi_vscode.vscode.adapter import (                            # noqa: E402
    UNDEFINED, AdapterError, AdapterUnavailable)


class FakeAdapter:
    """Stands in for VsCodeAdapter; branches on inputs to exercise every shape."""

    def exec_js(self, code):
        if code == "undef":
            return UNDEFINED
        if code == "null":
            return None
        if code == "err":
            raise AdapterError("boom")
        if code == "down":
            raise AdapterUnavailable("nope")
        return 2

    def command(self, command_id, args=None):
        if command_id == "void":
            return UNDEFINED
        return {"ok": command_id, "args": args}

    def workspace_folders(self):
        return [{"uri": "file:///w", "name": "w", "index": 0}]

    def editor_active(self):
        return None                       # no active editor -> public null

    def show_message(self, message, type="info"):
        return UNDEFINED                  # dismissed, no selection

    def apis(self):
        return [{"category": "command", "method": "x", "parameters": []}]


def _raw(base, method, path, body=None, ctype=None):
    """Return (status, content_length_header, raw_bytes)."""
    data = None
    headers = {}
    if body is not None:
        if isinstance(body, str):
            data = body.encode("utf-8")
            headers["Content-Type"] = ctype or "text/plain"
        else:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, int(r.headers.get("Content-Length") or 0), r.read()
    except urllib.error.HTTPError as e:
        return e.code, int(e.headers.get("Content-Length") or 0), e.read()


def check(results, label, cond):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def main():
    results = []
    port = PortAllocator().resolve(None)
    server, _sm = create_server("127.0.0.1", port, adapter=FakeAdapter(),
                                restricted=False, gate_root="")
    threading.Thread(target=server.start, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # wait for health
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)

    try:
        # /exec — unwrapped value / null / empty-on-undefined
        st, n, body = _raw(base, "POST", "/exec", "value", "text/plain")
        check(results, "/exec value -> 200 b'2'", st == 200 and body == b"2")
        st, n, body = _raw(base, "POST", "/exec", "null", "text/plain")
        check(results, "/exec null -> 200 b'null'", st == 200 and body == b"null")
        st, n, body = _raw(base, "POST", "/exec", "undef", "text/plain")
        check(results, "/exec undefined -> 200 empty body",
              st == 200 and body == b"" and n == 0)

        # /exec error / unavailable
        st, n, body = _raw(base, "POST", "/exec", "err", "text/plain")
        check(results, "/exec adapter error -> 500",
              st == 500 and b'"error":"boom"' in body)
        st, n, body = _raw(base, "POST", "/exec", "down", "text/plain")
        check(results, "/exec adapter unavailable -> 503",
              st == 503 and b'"error":"vscode adapter not available"' in body)

        # /command — wrapped present value / omitted result for undefined
        st, n, body = _raw(base, "POST", "/command/do.thing", {"args": ["a"]})
        check(results, "/command present -> {success:true,result:...}",
              st == 200
              and body == b'{"success":true,"result":{"ok":"do.thing","args":["a"]}}')
        st, n, body = _raw(base, "POST", "/command/void", {"args": []})
        check(results, "/command void -> {success:true} (no result)",
              st == 200 and body == b'{"success":true}')
        check(results, "/command compact JSON (no spaces)", b'"success": true' not in body)

        # workspace / editor / showMessage / apis
        st, n, body = _raw(base, "GET", "/workspace/folders")
        check(results, "/workspace/folders passthrough",
              st == 200 and body == b'[{"uri":"file:///w","name":"w","index":0}]')
        st, n, body = _raw(base, "GET", "/editor/active")
        check(results, "/editor/active no-editor -> b'null'", st == 200 and body == b"null")
        st, n, body = _raw(base, "POST", "/window/showMessage",
                           {"message": "hi", "type": "info"})
        check(results, "/window/showMessage -> {success:true}",
              st == 200 and body == b'{"success":true}')
        st, n, body = _raw(base, "GET", "/apis")
        check(results, "/apis passthrough array",
              st == 200
              and body == b'[{"category":"command","method":"x","parameters":[]}]')

        # /term/* is served as of Phase 13D (its shaping is covered by
        # test_term_routes.py); here only a genuinely unknown route still 404s.
        st, n, body = _raw(base, "GET", "/nope/xyz")
        check(results, "unknown route -> 404", st == 404)

        # pure routes still work alongside the adapter-backed ones
        st, n, body = _raw(base, "GET", "/health")
        check(results, "/health still ok", st == 200 and b'"status":"ok"' in body)
    finally:
        server.stop()

    # ── Restricted-mode server: /exec and /command must return 403 ──
    port2 = PortAllocator().resolve(None)
    server2, _ = create_server("127.0.0.1", port2, adapter=FakeAdapter(),
                               restricted=True, gate_root="")
    threading.Thread(target=server2.start, daemon=True).start()
    base2 = f"http://127.0.0.1:{port2}"
    for _ in range(40):
        try:
            with urllib.request.urlopen(base2 + "/health", timeout=5) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)

    try:
        st, n, body = _raw(base2, "POST", "/exec", "value", "text/plain")
        check(results, "restricted /exec -> 403",
              st == 403 and b"security policy" in body)
        st, n, body = _raw(base2, "POST", "/command/do.thing", {"args": []})
        check(results, "restricted /command -> 403",
              st == 403 and b"security policy" in body)

        st, n, body = _raw(base2, "GET", "/workspace/folders")
        check(results, "restricted /workspace/folders still ok", st == 200)
        st, n, body = _raw(base2, "GET", "/health")
        check(results, "restricted /health still ok", st == 200)
    finally:
        server2.stop()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*44}")
    print(f"Phase 12E routes: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
