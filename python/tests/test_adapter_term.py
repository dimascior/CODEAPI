#!/usr/bin/env python3
"""Phase 13C acceptance — Python adapter TERMINAL client methods, WITHOUT VS Code.

A fake /vscode/term/* bridge (loopback) proves: all six term paths are called
correctly, the token rides every terminal call, alive/exitCode pass through,
raw buffer passes through, wrote passes through, delete returns killed, a bridge
404 becomes AdapterError (not AdapterUnavailable), and an unreachable bridge
becomes AdapterUnavailable.

    python python/tests/test_adapter_term.py
"""

import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.vscode.adapter import (          # noqa: E402
    AdapterError, AdapterUnavailable, VsCodeAdapter)

TOKEN = "term-token-xyz"
REQUESTS = []
_TERM_RE = re.compile(r"^/vscode/term/([^/]+)(/write|/read|/resize)?$")


class _TermBridge(BaseHTTPRequestHandler):
    def log_message(self, *a):
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
        path = self.path.split("?")[0]
        if method == "GET" and path == "/vscode/term/list":
            return self._send(200, {"ok": True, "value": [
                {"id": "t1", "pid": 111, "bytes": 5, "shell": "pwsh.exe",
                 "alive": True, "exitCode": None},
                {"id": "t2", "pid": 222, "bytes": 9, "shell": "cmd.exe",
                 "alive": False, "exitCode": -1073741510},
            ]})
        if method == "POST" and path == "/vscode/term/create":
            return self._send(200, {"ok": True, "value": {
                "id": "tNEW", "shell": "powershell.exe", "cwd": "C:\\x", "pid": 333}})
        m = _TERM_RE.match(path)
        if m:
            tid, sub = m.group(1), m.group(2)
            if tid == "ghost":
                return self._send(404, {"error": f"Terminal {tid} not found"})
            if method == "POST" and sub == "/write":
                d = json.loads(body or "{}")
                return self._send(200, {"ok": True, "value": {
                    "id": tid, "wrote": len(d.get("data") or "")}})
            if method == "GET" and sub == "/read":
                return self._send(200, {"ok": True, "value": {
                    "id": tid, "bytes": 42, "buffer": "\x1b[0mraw-ansi"}})
            if method == "POST" and sub == "/resize":
                d = json.loads(body or "{}")
                return self._send(200, {"ok": True, "value": {
                    "id": tid, "cols": d.get("cols"), "rows": d.get("rows")}})
            if method == "DELETE" and not sub:
                return self._send(200, {"ok": True, "value": {"id": tid, "killed": True}})
        return self._send(404, {"error": f"Cannot {method} {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


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
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TermBridge)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        a = VsCodeAdapter(port=port, token=TOKEN)

        tl = a.term_list()
        check(results, "term_list passes alive/exitCode through verbatim",
              isinstance(tl, list) and len(tl) == 2
              and tl[0]["alive"] is True and tl[0]["exitCode"] is None
              and tl[1]["alive"] is False and tl[1]["exitCode"] == -1073741510)
        # shell is the terminal's immutable spawn-time dialect source. The
        # adapter must carry it through untouched — `term run` picks its
        # execution envelope from this value, so a dropped or rewritten shell
        # means PowerShell syntax submitted to some other interpreter.
        check(results, "term_list passes shell through verbatim, unrewritten",
              tl[0]["shell"] == "pwsh.exe" and tl[1]["shell"] == "cmd.exe")

        tc = a.term_create(cwd="C:\\x", show=True)
        check(results, "term_create -> {id,shell,cwd,pid}",
              tc == {"id": "tNEW", "shell": "powershell.exe", "cwd": "C:\\x", "pid": 333})

        tw = a.term_write("t1", "echo hi", enter=True)
        check(results, "term_write -> wrote (bytes), not success",
              tw == {"id": "t1", "wrote": len("echo hi")})

        tr = a.term_read("t1", tail=100, clear=True)
        check(results, "term_read preserves raw buffer",
              tr == {"id": "t1", "bytes": 42, "buffer": "\x1b[0mraw-ansi"})

        trs = a.term_resize("t1", 120, 40)
        check(results, "term_resize -> {id,cols,rows}",
              trs == {"id": "t1", "cols": 120, "rows": 40})

        td = a.term_delete("t1")
        check(results, "term_delete -> {id,killed}", td == {"id": "t1", "killed": True})

        term_reqs = [r for r in REQUESTS if "/vscode/term" in r["path"]]
        paths = {(r["method"], r["path"].split("?")[0]) for r in term_reqs}
        for exp in [("GET", "/vscode/term/list"), ("POST", "/vscode/term/create"),
                    ("POST", "/vscode/term/t1/write"), ("GET", "/vscode/term/t1/read"),
                    ("POST", "/vscode/term/t1/resize"), ("DELETE", "/vscode/term/t1")]:
            check(results, f"hit {exp[0]} {exp[1]}", exp in paths)

        check(results, "token on every terminal call",
              len(term_reqs) > 0 and all(r["token"] == TOKEN for r in term_reqs))

        read_req = next((r for r in term_reqs if "/read" in r["path"]), None)
        check(results, "term_read sends tail & clear query",
              read_req is not None and "tail=100" in read_req["path"]
              and "clear=true" in read_req["path"])

        try:
            a.term_delete("ghost")
            check(results, "bridge 404 -> AdapterError", False)
        except AdapterUnavailable:
            check(results, "bridge 404 -> AdapterError", False)
        except AdapterError as e:
            check(results, "bridge 404 -> AdapterError", "not found" in str(e))
    finally:
        server.shutdown()

    dead = VsCodeAdapter(port=_free_port(), token=TOKEN, timeout=2.0)
    try:
        dead.term_list()
        check(results, "unreachable -> AdapterUnavailable", False)
    except AdapterUnavailable:
        check(results, "unreachable -> AdapterUnavailable", True)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*44}")
    print(f"Phase 13C adapter terminal: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
