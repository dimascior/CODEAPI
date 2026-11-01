#!/usr/bin/env python3
"""CLI-level acceptance — the actual `python -m codeapi_vscode.runtime`.

Complements test_runtime_pure.py (which tests in-process). Here we launch the
runtime as a real subprocess to prove the CLI invocation itself:
  #1 --port 3639 refuses with exit 2 (window band, config error)
  #2 an EXPLICIT free port binds EXACTLY that port (no fall-forward)
  #3 an EXPLICIT port that is already occupied exits 3 with a distinct
     "port allocation error" — the probe->bind race case: the launching adapter
     keys on exit 3 to retry its next candidate (never a silent different port)
  #4 NO port at all scans the adapter band and starts
  #5 /session serverPort == the bound port

No VS Code required.

    python python/tests/test_runtime_cli.py
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(HERE)                 # .../python  (holds codeapi_vscode/)
MOD = [sys.executable, "-m", "codeapi_vscode.runtime"]

# A clean env: the adapter-injected port must not leak into the no-port case.
ENV = {k: v for k, v in os.environ.items() if k != "CODEAPI_PY_PORT"}


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


def _free_port():
    """Find a currently-free adapter-band port (probe like the JS adapter does)."""
    for p in range(47337, 47401):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("adapter band exhausted on this machine")


def _await_listen_port(proc, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            return None
        m = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def _reap(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def guard_check(results):
    p = subprocess.run(MOD + ["--port", "3639"], cwd=PKG_PARENT, env=ENV,
                       capture_output=True, text=True, timeout=15)
    ok = p.returncode == 2 and "refusing to bind" in p.stderr
    results.append(("CLI --port 3639 refuses (exit 2)", ok))
    print(f"{'OK  ' if ok else 'FAIL'}  CLI --port 3639 refuses (exit 2) "
          f"(rc={p.returncode})")


def exact_bind_check(results):
    """#2: an explicit free port binds exactly — never a different port."""
    want = _free_port()
    proc = subprocess.Popen(MOD + ["--port", str(want)], cwd=PKG_PARENT, env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        got = _await_listen_port(proc)
        ok = got == want
        results.append((f"explicit --port binds EXACTLY that port", ok))
        print(f"{'OK  ' if ok else 'FAIL'}  explicit --port {want} binds exactly "
              f"(got={got})")
    finally:
        _reap(proc)


def occupied_exact_check(results):
    """#3: the adversarial allocator race — the requested port is occupied
    (as if a racer grabbed it between the adapter's probe and our bind).
    The runtime must exit 3 with the distinct allocation error, NOT fall
    forward to another port."""
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        taken = _free_port()
        holder.bind(("127.0.0.1", taken))
        holder.listen(1)
        p = subprocess.run(MOD + ["--port", str(taken)], cwd=PKG_PARENT, env=ENV,
                           capture_output=True, text=True, timeout=15)
        ok = (p.returncode == 3 and "port allocation error" in p.stderr
              and "listening on" not in p.stdout)
        results.append(("occupied explicit port -> exit 3, no fall-forward", ok))
        print(f"{'OK  ' if ok else 'FAIL'}  occupied --port {taken} -> exit 3, "
              f"no fall-forward (rc={p.returncode})")
    finally:
        holder.close()


def scan_check(results):
    """#4 + #5: no port at all -> scans the band; /session echoes the bound port."""
    proc = subprocess.Popen(MOD, cwd=PKG_PARENT, env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        port = _await_listen_port(proc)
        started = port is not None and port in range(47337, 47401)
        results.append(("no --port scans the adapter band and starts", started))
        print(f"{'OK  ' if started else 'FAIL'}  no --port scans the band "
              f"(port={port})")

        health_ok = session_ok = False
        if port is not None:
            st, _ = _get(f"http://127.0.0.1:{port}", "/health")
            health_ok = st == 200
            st, p = _get(f"http://127.0.0.1:{port}", "/session")
            session_ok = st == 200 and p is not None and p.get("serverPort") == port
        results.append(("CLI runtime serves /health", health_ok))
        print(f"{'OK  ' if health_ok else 'FAIL'}  CLI runtime serves /health")
        results.append(("CLI /session serverPort == bound port", session_ok))
        print(f"{'OK  ' if session_ok else 'FAIL'}  CLI /session serverPort == bound port")
    finally:
        _reap(proc)


def main():
    results = []
    guard_check(results)
    exact_bind_check(results)
    occupied_exact_check(results)
    scan_check(results)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*44}")
    print(f"runtime CLI: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
