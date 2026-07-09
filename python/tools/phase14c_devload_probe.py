#!/usr/bin/env python3
"""Phase 14C — read-only discovery of the dev-loaded adapter's Python runtime.

Determines, WITHOUT creating any terminal or mutating any state, whether a
dev-loaded extension-adapter is live. The dev-loaded adapter must launch the Python
runtime OUTSIDE the VS Code window band (47337+); the legacy sourceless bundle serves
on 3637-3646. This probe:

  - scans 47337-47346 for a Python runtime (GET /health, /session, /apis) and, if one
    is found, checks whether adapter-backed /exec reaches a REAL bridge (not 503) via a
    single pure `return typeof vscode` probe (no side effects);
  - scans 3637-3646 for the legacy runtime (GET /health, /session) to confirm the
    comparison baseline exists.

Pure HTTP only: GET /health, /session, /apis; one POST /exec with a pure typeof body.
No /term/create, no writes, no kills. Prints a discovery report and exits 0.

    python python/tools/phase14c_devload_probe.py
"""

import json
import sys
import urllib.error
import urllib.request

DEVLOAD_BAND = range(47337, 47347)     # where a dev-loaded adapter must bind
WINDOW_BAND = range(3637, 3647)        # legacy sourceless bundle


def _get(base, path, timeout=2):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  (connection refused / timeout / etc.)
        return None, None


def _exec(base, code, timeout=5):
    req = urllib.request.Request(base + "/exec", data=code.encode("utf-8"),
                                 method="POST", headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (raw.decode("utf-8", "replace") if raw else "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return None, f"<{type(e).__name__}>"


def scan(band, label):
    found = []
    for p in band:
        base = f"http://127.0.0.1:{p}"
        st, health = _get(base, "/health")
        if st == 200:
            _, sess = _get(base, "/session")
            server_port = sess.get("serverPort") if isinstance(sess, dict) else None
            found.append((p, server_port, base))
            print(f"  [{label}] {base}/health -> 200  serverPort={server_port}")
    if not found:
        print(f"  [{label}] no runtime answered /health in "
              f"{band.start}-{band.stop - 1}")
    return found


def main():
    print("### Phase 14C dev-load discovery (read-only) ###\n")

    print("A. Dev-loaded band 47337-47346 (adapter-launched Python runtime):")
    devload = scan(DEVLOAD_BAND, "devload")

    print("\nB. Legacy window band 3637-3646 (sourceless bundle):")
    legacy = scan(WINDOW_BAND, "legacy")

    print("\nC. Dev-loaded adapter reachability:")
    if not devload:
        print("  NO dev-loaded Python runtime on 47337+.")
        print("  => The extension-adapter is NOT dev-loaded; the real adapter bridge")
        print("     (vscode / node-pty / codeApiTerm) is not reachable from Python.")
        verdict = "BLOCKED"
    else:
        verdict = "REACHABLE"
        for port, server_port, base in devload:
            print(f"  probing {base} adapter-backed /exec (pure typeof vscode)...")
            st, body = _exec(base, "return typeof vscode")
            print(f"    POST /exec 'return typeof vscode' -> {st} {body!r}")
            if st == 503:
                print("    -> 503: runtime is up but adapter UNAVAILABLE "
                      "(not the dev-loaded bridge).")
                verdict = "RUNTIME_NO_ADAPTER"
            elif st == 200 and body == '"object"':
                print("    -> real dev-loaded bridge reached (vscode is an object).")
            _, apis = _get(base, "/apis")
            if isinstance(apis, list):
                cats = {}
                for e in apis:
                    c = e.get("category") if isinstance(e, dict) else None
                    cats[c] = cats.get(c, 0) + 1
                print(f"    GET /apis -> {len(apis)} entries, categories="
                      f"{dict(sorted((str(k), v) for k, v in cats.items()))}")

    print(f"\nVERDICT: {verdict}")
    print(f"devload_runtimes={[p for p, _, _ in devload]}  "
          f"legacy_runtimes={[p for p, _, _ in legacy]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
