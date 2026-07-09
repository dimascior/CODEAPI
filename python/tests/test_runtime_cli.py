#!/usr/bin/env python3
"""Phase 11 CLI-level acceptance — the actual `python -m codeapi_vscode.runtime`.

Complements test_runtime_pure.py (which tests in-process). Here we launch the
runtime as a real subprocess to prove the CLI invocation itself:
  #1 --port 3639 refuses with exit 2
  #2 --port 47337 starts and serves /health
  #4 /session serverPort == the bound port

No VS Code required.

    python python/tests/test_runtime_cli.py
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(HERE)                 # .../python  (holds codeapi_vscode/)
MOD = [sys.executable, "-m", "codeapi_vscode.runtime"]


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


def guard_check(results):
    p = subprocess.run(MOD + ["--port", "3639"], cwd=PKG_PARENT,
                       capture_output=True, text=True, timeout=15)
    ok = p.returncode == 2 and "refusing to bind" in p.stderr
    results.append(("CLI --port 3639 refuses (exit 2)", ok))
    print(f"{'OK  ' if ok else 'FAIL'}  CLI --port 3639 refuses (exit 2) "
          f"(rc={p.returncode})")


def start_check(results):
    proc = subprocess.Popen(MOD + ["--port", "47337"], cwd=PKG_PARENT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    port = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            m = re.search(r"listening on http://127\.0\.0\.1:(\d+)", line)
            if m:
                port = int(m.group(1))
                break
        started = port is not None
        results.append(("CLI --port 47337 starts and prints listen port", started))
        print(f"{'OK  ' if started else 'FAIL'}  CLI --port 47337 starts "
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
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    results = []
    guard_check(results)
    start_check(results)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*44}")
    print(f"Phase 11 CLI: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
