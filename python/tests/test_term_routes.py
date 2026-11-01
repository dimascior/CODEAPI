#!/usr/bin/env python3
"""Phase 13D acceptance — public /term/* route shaping, WITHOUT VS Code.

Runs the REAL Tier-2 server with a FAKE adapter injected and byte-checks the public
/term/* shapes, the unknown-id 404, other-error 500, unknown-route 404, and — with a
separate UNCONFIGURED real adapter — the 503 availability path. Python never infers
liveness: alive/exitCode pass straight through; wrote is bytes accepted.

    python python/tests/test_term_routes.py
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
from codeapi_vscode.vscode.adapter import AdapterError, VsCodeAdapter  # noqa: E402

READ_CALLS = []


class FakeAdapter:
    def term_list(self):
        # The real adapter emits the surface discriminator alongside alive/exitCode;
        # the public route passes it through verbatim (Python never invents it).
        # `shell` is the terminal's immutable spawn-time dialect source; the
        # route must carry it through untouched, because `term run` selects
        # its execution envelope from it.
        return [{"id": "t1", "pid": 111, "bytes": 5, "shell": "pwsh.exe",
                 "generation": "g-aaa", "alive": True, "exitCode": None,
                 "surface": "vscode-terminal"},
                {"id": "t2", "pid": 222, "bytes": 9, "shell": "cmd.exe",
                 "generation": "g-bbb", "alive": False,
                 "exitCode": -1073741510, "surface": "process"}]

    def term_create(self, id=None, cwd=None, shell=None, args=None, cols=None,
                    rows=None, max_bytes=None, show=None):
        # show truthy -> the adapter surfaces the pty as a VS Code integrated terminal
        # and stamps surface:vscode-terminal; the route carries it through unchanged.
        return {"id": "tNEW", "shell": "powershell.exe", "cwd": "C:/x", "pid": 333,
                "surface": "vscode-terminal"}

    def term_write(self, term_id, data, *, enter):
        # Mirrors the real adapter's REQUIRED keyword-only `enter`. A fake
        # that kept a default would absorb a route-side omission silently.
        return {"id": term_id, "wrote": len(data or "")}

    def term_read(self, term_id, tail=None, clear=False):
        READ_CALLS.append((term_id, tail, clear))
        return {"id": term_id, "bytes": 42, "buffer": "\x1b[0mraw-ansi"}

    def term_resize(self, term_id, cols, rows):
        return {"id": term_id, "cols": cols, "rows": rows}

    def term_delete(self, term_id):
        if term_id == "ghost":
            raise AdapterError("Terminal ghost not found")   # -> public 404
        if term_id == "boom":
            raise AdapterError("kaboom")                     # -> public 500
        return {"id": term_id, "killed": True}


def _raw(base, method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check(results, label, cond):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def _wait(base):
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)


def main():
    results = []
    port = PortAllocator().resolve(None)
    server, _ = create_server("127.0.0.1", port, adapter=FakeAdapter())
    threading.Thread(target=server.start, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _wait(base)

    try:
        st, body = _raw(base, "GET", "/term/list")
        check(results, "/term/list passes shell/generation/alive/exitCode/surface",
              st == 200 and body == b'[{"id":"t1","pid":111,"bytes":5,'
              b'"shell":"pwsh.exe","generation":"g-aaa","alive":true,'
              b'"exitCode":null,"surface":"vscode-terminal"},'
              b'{"id":"t2","pid":222,"bytes":9,"shell":"cmd.exe",'
              b'"generation":"g-bbb","alive":false,"exitCode":-1073741510,'
              b'"surface":"process"}]')
        check(results, "/term/list shell survives byte-for-byte, both rows",
              [r.get("shell") for r in json.loads(body)]
              == ["pwsh.exe", "cmd.exe"])
        check(results, "/term/list generation survives byte-for-byte, both rows",
              [r.get("generation") for r in json.loads(body)]
              == ["g-aaa", "g-bbb"])
        check(results, "/term/list compact JSON (no spaces)", b'": ' not in body)
        rows = json.loads(body)
        check(results, "/term/list surface+alive+exitCode preserved together",
              rows[0]["surface"] == "vscode-terminal" and rows[0]["alive"] is True
              and rows[0]["exitCode"] is None
              and rows[1]["surface"] == "process" and rows[1]["alive"] is False
              and rows[1]["exitCode"] == -1073741510)

        st, body = _raw(base, "POST", "/term/create", {"cwd": "C:/x", "show": True})
        check(results, "/term/create -> {id,shell,cwd,pid,surface}",
              st == 200
              and body == b'{"id":"tNEW","shell":"powershell.exe","cwd":"C:/x",'
              b'"pid":333,"surface":"vscode-terminal"}')
        check(results, "/term/create surface passes through the public route",
              json.loads(body).get("surface") == "vscode-terminal")

        st, body = _raw(base, "POST", "/term/t1/write",
                        {"data": "echo hi", "enter": True})
        check(results, "/term/:id/write -> {id,wrote} (bytes, not liveness)",
              st == 200 and body == b'{"id":"t1","wrote":7}')

        st, body = _raw(base, "GET", "/term/t1/read")
        check(results, "/term/:id/read raw buffer exact",
              st == 200
              and json.loads(body) == {"id": "t1", "bytes": 42, "buffer": "\x1b[0mraw-ansi"})

        st, body = _raw(base, "GET", "/term/t1/read?tail=100&clear=true")
        check(results, "/term/:id/read?tail&clear passes tail+clear",
              st == 200 and READ_CALLS[-1] == ("t1", 100, True))

        st, body = _raw(base, "POST", "/term/t1/resize", {"cols": 120, "rows": 40})
        check(results, "/term/:id/resize -> {id,cols,rows}",
              st == 200 and body == b'{"id":"t1","cols":120,"rows":40}')

        st, body = _raw(base, "DELETE", "/term/t1")
        check(results, "DELETE /term/:id -> {id,killed}",
              st == 200 and body == b'{"id":"t1","killed":true}')

        st, body = _raw(base, "DELETE", "/term/ghost")
        check(results, "unknown terminal id -> 404 Terminal <id> not found",
              st == 404 and json.loads(body) == {"error": "Terminal ghost not found"})

        st, body = _raw(base, "DELETE", "/term/boom")
        check(results, "other adapter error -> 500",
              st == 500 and json.loads(body) == {"error": "kaboom"})

        st, body = _raw(base, "GET", "/definitely/unknown")
        check(results, "unknown unrelated route -> 404", st == 404)
    finally:
        server.stop()

    # 503 when the adapter is unavailable (unconfigured real adapter).
    os.environ.pop("CODEAPI_EXTENSION_PORT", None)
    os.environ.pop("CODEAPI_BRIDGE_TOKEN", None)
    port2 = PortAllocator().resolve(None)
    server2, _ = create_server("127.0.0.1", port2, adapter=VsCodeAdapter(port=None))
    threading.Thread(target=server2.start, daemon=True).start()
    base2 = f"http://127.0.0.1:{port2}"
    _wait(base2)
    try:
        st, body = _raw(base2, "GET", "/term/list")
        check(results, "adapter unavailable -> 503",
              st == 503 and json.loads(body) == {"error": "vscode adapter not available"})
    finally:
        server2.stop()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*44}")
    print(f"Phase 13D term routes: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
