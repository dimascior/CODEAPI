#!/usr/bin/env python3
"""Phase 11 acceptance — the pure-route runtime, WITHOUT VS Code.

Starts the runtime in-process on a free out-of-band port and validates every pure
route against docs/WIRE_CONTRACT.md. No extension, no vscode, no window band.

    python python/tests/test_runtime_pure.py
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.runtime import RuntimeConfigError, create_server, parse_config
from codeapi_vscode.session.port_allocator import PortAllocator


def _req(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return e.code, payload


def _raw(base, path):
    """Return the raw response BYTES (not parsed) — to pin byte compactness."""
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.read()


def check(results, label, cond):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


def main():
    results = []

    # Force the default adapter UNCONFIGURED so adapter-backed routes answer 503
    # (this is the pure-runtime test; there is no JS adapter present).
    os.environ.pop("CODEAPI_EXTENSION_PORT", None)
    os.environ.pop("CODEAPI_BRIDGE_TOKEN", None)

    # Acceptance #1: config guard refuses the VS Code window band.
    try:
        parse_config(["--port", "3639"])
        check(results, "guard refuses --port 3639", False)
    except RuntimeConfigError:
        check(results, "guard refuses --port 3639", True)

    # Acceptance #1b: allocation and discovery share ONE band (47337-47400).
    # A preferred port outside it is refused (it would be undiscoverable), and
    # exhaustion of the whole band raises visibly instead of overflowing.
    from codeapi_vscode.session.port_allocator import (PortAllocationError,
                                                       RESERVED_BAND)
    check(results, "allocator band == discovery ADAPTER_RANGE (47337-47400)",
          RESERVED_BAND == range(47337, 47401))
    try:
        PortAllocator().resolve(47401)
        check(results, "allocator refuses out-of-band preferred (47401)", False)
    except PortAllocationError as e:
        check(results, "allocator refuses out-of-band preferred (47401)",
              "undiscoverable" in str(e))
    _orig_free = PortAllocator._free
    try:
        PortAllocator._free = staticmethod(lambda p: False)   # every port "busy"
        try:
            PortAllocator().resolve(None)
            check(results, "allocator exhaustion raises visibly (65th runtime)", False)
        except PortAllocationError as e:
            check(results, "allocator exhaustion raises visibly (65th runtime)",
                  "exhausted" in str(e) and "64 ports" in str(e))
    finally:
        # re-wrap: accessing a staticmethod on the class yields the bare function
        PortAllocator._free = staticmethod(_orig_free)

    # Start the runtime in-process on a free reserved-band port (no VS Code).
    port = PortAllocator().resolve(None)
    check(results, f"port allocated outside window band ({port})",
          port not in range(3637, 3647))
    server, sm = create_server("127.0.0.1", port)
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    try:
        # wait for health
        up = False
        for _ in range(40):
            try:
                st, _p = _req(base, "GET", "/health")
                if st == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        check(results, "runtime starts and serves /health", up)

        # #3 GET /health shape
        st, p = _req(base, "GET", "/health")
        check(results, "/health shape",
              st == 200 and p.get("status") == "ok"
              and isinstance(p.get("timestamp"), str) and p["timestamp"].endswith("Z"))

        # #3b raw bytes: compact JSON (no spaces), matching the extension's
        # res.json(). Inspects BYTES, not parsed shape. Fails on spaced JSON.
        body = _raw(base, "/health")
        check(results, "/health compact JSON bytes",
              b'"status":"ok"' in body and b'"status": "ok"' not in body)

        # #4 GET /session shape, serverPort == bound port. The adapter-pinned
        # identity fields (incl. codeapiWorkspaceId/codeapiRuntimeId) are additive
        # and present-as-None in the standalone runtime (no CODEAPI_* identity env).
        st, p = _req(base, "GET", "/session")
        keys = {"id", "pid", "workspaceUri", "windowId", "capabilities",
                "lastSeen", "serverPort", "meshPeers",
                "vscodeSessionId", "extensionHostPid", "codeapiWindowId",
                "codeapiWorkspaceId", "codeapiRuntimeId"}
        check(results, "/session shape",
              st == 200 and set(p.keys()) == keys
              and p["capabilities"] == [] and p["meshPeers"] == []
              and p["serverPort"] == port and p["pid"] == os.getpid())
        check(results, "/session identity fields None in standalone",
              p["vscodeSessionId"] is None and p["extensionHostPid"] is None
              and p["codeapiWindowId"] is None and p["codeapiWorkspaceId"] is None
              and p["codeapiRuntimeId"] is None)

        # #5 dynamic endpoint add/get/remove round trip
        st, p = _req(base, "POST", "/add-endpoint",
                     {"path": "/wire-probe", "response": [1, 2, 3]})
        check(results, "/add-endpoint shape",
              st == 200 and p == {"success": True,
                                  "message": "Endpoint /wire-probe registered."})
        st, p = _req(base, "GET", "/wire-probe")
        check(results, "dynamic GET shape",
              st == 200 and p == {"success": True, "dynamic": True,
                                  "path": "/wire-probe", "response": [1, 2, 3]})
        st, p = _req(base, "POST", "/remove-endpoint", {"path": "/wire-probe"})
        check(results, "/remove-endpoint shape",
              st == 200 and p == {"success": True,
                                  "message": "Endpoint /wire-probe removed."})
        st, p = _req(base, "GET", "/wire-probe")
        check(results, "dynamic GET 404 after remove", st == 404)
        st, p = _req(base, "POST", "/remove-endpoint", {"path": "/nope"})
        check(results, "/remove-endpoint not-found 404",
              st == 404 and isinstance(p, dict) and "not found" in str(p.get("error", "")).lower())

        # #6 /mesh/peers
        st, p = _req(base, "GET", "/mesh/peers")
        check(results, "/mesh/peers == []", st == 200 and p == [])

        # #7 /mesh/broadcast/:endpoint
        st, p = _req(base, "POST", "/mesh/broadcast/health", {})
        check(results, "/mesh/broadcast shape",
              st == 200 and p == {"success": True, "results": []})

        # #8 no-scope-creep (Phase 12): adapter-backed routes are SERVED but return
        # 503 when no adapter is available; terminal routes remain 404. Hit each with
        # its real method so it reaches the handler (not a method-mismatch 404).
        adapter_backed = [
            ("GET", "/workspace/folders", None),
            ("GET", "/editor/active", None),
            ("POST", "/window/showMessage", {"message": "x", "type": "info"}),
            ("GET", "/apis", None),
        ]
        for method, route, rbody in adapter_backed:
            st, p = _req(base, method, route, rbody)
            check(results, f"adapter-backed {method} {route} -> 503",
                  st == 503 and isinstance(p, dict)
                  and p.get("error") == "vscode adapter not available")

        # The default restricted policy refuses /exec and /command before any adapter
        # check. test_vscode_routes.py covers them with restricted=False.
        restricted_routes = [
            ("POST", "/exec", "return 1"),
            ("POST", "/command/foo", {"args": []}),
        ]
        for method, route, rbody in restricted_routes:
            st, p = _req(base, method, route, rbody)
            check(results, f"restricted {method} {route} -> 403",
                  st == 403 and isinstance(p, dict)
                  and p.get("error") == "route disabled by security policy")

        # terminal routes are SERVED as of Phase 13D: 503 when no adapter is
        # configured (NOT 404). Hit each with its real method so it reaches the
        # handler (not a method-mismatch 404).
        term_checks = [
            ("GET", "/term/list", None),
            ("POST", "/term/create", {}),
            ("POST", "/term/t1/write", {"data": "x"}),
            ("GET", "/term/t1/read", None),
            ("POST", "/term/t1/resize", {"cols": 80, "rows": 24}),
            ("DELETE", "/term/t1", None),
        ]
        for method, route, rbody in term_checks:
            st, p = _req(base, method, route, rbody)
            check(results, f"terminal {method} {route} -> 503 (no adapter)",
                  st == 503 and isinstance(p, dict)
                  and p.get("error") == "vscode adapter not available")

        # unknown unrelated routes still 404
        st, _p = _req(base, "GET", "/definitely/unknown")
        check(results, "unknown route -> 404", st == 404)

    finally:
        server.stop()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*44}")
    print(f"Phase 11 runtime: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
