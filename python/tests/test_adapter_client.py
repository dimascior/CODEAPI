#!/usr/bin/env python3
"""Phase 12D acceptance — the Python adapter client, WITHOUT VS Code.

Stands up a FAKE adapter bridge (a loopback HTTP server returning canned envelopes)
and asserts VsCodeAdapter's behavior: token on every request, envelope semantics
(value present / omitted / null / error), method routing, and the unreachable /
unconfigured failure modes. This is the bridge-unit strategy from
docs/ADAPTER_BRIDGE_CONTRACT.md §9.1.

    python python/tests/test_adapter_client.py
"""

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.vscode.adapter import (          # noqa: E402
    UNDEFINED, AdapterError, AdapterUnavailable, VsCodeAdapter)

TOKEN = "test-token-abc123"
REQUESTS = []   # every request the fake bridge saw (for token / routing assertions)


class _FakeBridge(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        body = self._read()
        token = self.headers.get("X-CODEAPI-BRIDGE-TOKEN")
        REQUESTS.append({"method": method, "path": self.path,
                         "token": token, "body": body.decode() or ""})
        if token != TOKEN:
            return self._send(403, {"error": "forbidden"})
        p = self.path
        if method == "POST" and p == "/vscode/exec":
            code = json.loads(body or "{}").get("code", "")
            if "THROW" in code:
                return self._send(500, {"error": "boom"})
            if "undefined" in code:
                return self._send(200, {"ok": True})               # omitted value
            if "null" in code:
                return self._send(200, {"ok": True, "value": None})  # explicit null
            return self._send(200, {"ok": True, "value": 2})
        if method == "POST" and p == "/vscode/command":
            parsed = json.loads(body or "{}")
            if parsed.get("id") == "void.cmd":
                return self._send(200, {"ok": True})
            return self._send(200, {"ok": True,
                                    "value": {"id": parsed.get("id"),
                                              "args": parsed.get("args")}})
        if method == "GET" and p == "/vscode/workspace/folders":
            return self._send(200, {"ok": True,
                                    "value": [{"uri": "file:///w", "name": "w", "index": 0}]})
        if method == "GET" and p == "/vscode/editor/active":
            return self._send(200, {"ok": True, "value": None})
        if method == "POST" and p == "/vscode/window/message":
            parsed = json.loads(body or "{}")
            # echo the type back so the test can confirm it was forwarded
            return self._send(200, {"ok": True, "value": parsed.get("type")})
        if method == "GET" and p == "/vscode/apis":
            return self._send(200, {"ok": True,
                                    "value": [{"category": "command",
                                               "method": "x", "parameters": []}]})
        return self._send(404, {"error": f"Cannot {method} {p}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check(results, label, cond):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def main():
    results = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBridge)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        a = VsCodeAdapter(port=port, token=TOKEN)

        # envelope semantics -------------------------------------------------
        check(results, "exec_js present value -> 2", a.exec_js("return 1+1") == 2)
        check(results, "exec_js undefined -> UNDEFINED", a.exec_js("return undefined") is UNDEFINED)
        check(results, "exec_js null -> None (present)", a.exec_js("return null") is None)

        try:
            a.exec_js("return THROW")
            check(results, "error envelope -> AdapterError", False)
        except AdapterError as e:
            check(results, "error envelope -> AdapterError", "boom" in str(e))

        # method routing + shapes -------------------------------------------
        cmd = a.command("do.thing", ["x", 1])
        check(results, "command routes {id,args} + returns value",
              cmd == {"id": "do.thing", "args": ["x", 1]})
        check(results, "command void -> UNDEFINED", a.command("void.cmd") is UNDEFINED)
        check(results, "workspace_folders -> array",
              a.workspace_folders() == [{"uri": "file:///w", "name": "w", "index": 0}])
        check(results, "editor_active no-editor -> None", a.editor_active() is None)
        check(results, "show_message forwards type + returns value",
              a.show_message("hi", "warning") == "warning")
        check(results, "apis -> array of descriptors",
              isinstance(a.apis(), list) and a.apis()[0]["category"] == "command")

        # token on EVERY request --------------------------------------------
        check(results, "token sent on every request",
              len(REQUESTS) > 0 and all(r["token"] == TOKEN for r in REQUESTS))

        # correct paths + methods hit --------------------------------------
        paths = {(r["method"], r["path"]) for r in REQUESTS}
        for expected in [("POST", "/vscode/exec"), ("POST", "/vscode/command"),
                         ("GET", "/vscode/workspace/folders"),
                         ("GET", "/vscode/editor/active"),
                         ("POST", "/vscode/window/message"), ("GET", "/vscode/apis")]:
            check(results, f"hit {expected[0]} {expected[1]}", expected in paths)

        # wrong token -> 403 -> AdapterError forbidden ----------------------
        bad = VsCodeAdapter(port=port, token="wrong")
        try:
            bad.exec_js("return 1")
            check(results, "wrong token -> AdapterError forbidden", False)
        except AdapterError as e:
            check(results, "wrong token -> AdapterError forbidden", "forbidden" in str(e))

        # unreachable -> AdapterUnavailable ---------------------------------
        dead = VsCodeAdapter(port=_free_port(), token=TOKEN, timeout=2.0)
        try:
            dead.exec_js("return 1")
            check(results, "unreachable -> AdapterUnavailable", False)
        except AdapterUnavailable:
            check(results, "unreachable -> AdapterUnavailable", True)
        except Exception as e:  # noqa: BLE001
            check(results, "unreachable -> AdapterUnavailable",
                  False) or print(f"      (got {type(e).__name__})")

        # unconfigured (no port, env unset) -> AdapterUnavailable -----------
        saved = os.environ.pop("CODEAPI_EXTENSION_PORT", None)
        try:
            VsCodeAdapter(port=None, token=TOKEN).exec_js("return 1")
            check(results, "unconfigured -> AdapterUnavailable", False)
        except AdapterUnavailable:
            check(results, "unconfigured -> AdapterUnavailable", True)
        finally:
            if saved is not None:
                os.environ["CODEAPI_EXTENSION_PORT"] = saved

        # env-based construction --------------------------------------------
        os.environ["CODEAPI_EXTENSION_PORT"] = str(port)
        os.environ["CODEAPI_BRIDGE_TOKEN"] = TOKEN
        try:
            check(results, "constructs from env vars",
                  VsCodeAdapter().exec_js("return 1+1") == 2)
        finally:
            os.environ.pop("CODEAPI_EXTENSION_PORT", None)
            os.environ.pop("CODEAPI_BRIDGE_TOKEN", None)

    finally:
        server.shutdown()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*44}")
    print(f"Phase 12D adapter client: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
