#!/usr/bin/env python3
"""Axis 3 — Smoke test runner for the CODEAPI Python package.

Migrated from legacy-seed/cli/test_smoke.py (Phase 10). Invokes the CLI as
`python -m codeapi_vscode` (cwd = the package parent, so the package imports).

Usage:
    python python/tests/test_smoke.py                  # offline checks only
    python python/tests/test_smoke.py --port 3637      # full suite, live window
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(HERE)              # .../python  (holds codeapi_vscode/)
DIFF_TEST = os.path.join(HERE, "test_differential.py")
MOD = [sys.executable, "-m", "codeapi_vscode"]  # the forward CLI invocation


def run(label, cmd, cwd=None):
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"CMD:  {' '.join(cmd)}")
    print("-" * 60)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=cwd)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    passed = result.returncode == 0
    print(f"\n{'PASS' if passed else 'FAIL'}  {label} (exit={result.returncode})")
    return passed


def run_verified_smoke(port):
    """Create a temp PowerShell terminal, run Get-Location with run-verified,
    and check the evidence structure."""
    label = "run-verified evidence"
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print("-" * 60)

    create = subprocess.run(
        MOD + ["-p", port, "--json", "term", "create", "--shell", "powershell.exe"],
        capture_output=True, text=True, timeout=30, cwd=PKG_PARENT)
    if create.returncode != 0:
        print(f"FAIL  {label} — could not create terminal")
        print(create.stderr, file=sys.stderr)
        return False
    try:
        tid = json.loads(create.stdout)["id"]
    except (json.JSONDecodeError, KeyError):
        print(f"FAIL  {label} — no id in create output")
        return False

    import time
    time.sleep(1)

    rv = subprocess.run(
        MOD + ["-p", port, "--json", "term", "run-verified", tid, "Get-Location"],
        capture_output=True, text=True, timeout=60, cwd=PKG_PARENT)

    subprocess.run(
        MOD + ["-p", port, "term", "kill", tid],
        capture_output=True, text=True, timeout=10, cwd=PKG_PARENT)

    if rv.returncode != 0:
        print(f"FAIL  {label} — run-verified exited {rv.returncode}")
        print(rv.stdout)
        print(rv.stderr, file=sys.stderr)
        return False

    try:
        result = json.loads(rv.stdout)
    except json.JSONDecodeError:
        print(f"FAIL  {label} — output is not JSON")
        print(rv.stdout[:500])
        return False

    checks = []
    checks.append(("ok", result.get("ok") is True))
    comp = result.get("completion", {})
    checks.append(("start_marker_seen", comp.get("start_marker_seen") is True))
    checks.append(("end_marker_seen", comp.get("end_marker_seen") is True))
    checks.append(("timed_out=false", comp.get("timed_out") is False))
    st = result.get("status", {})
    checks.append(("exit_code=0", st.get("exit_code") == 0))
    before_cwd = (result.get("terminal_before") or {}).get("cwd")
    after_cwd = (result.get("terminal_after") or {}).get("cwd")
    checks.append(("cwd_preserved",
                    before_cwd is not None and before_cwd == after_cwd))

    all_ok = True
    for name, passed in checks:
        tag = "OK" if passed else "FAIL"
        print(f"  [{tag}] {name}")
        if not passed:
            all_ok = False
    print(f"\n{'PASS' if all_ok else 'FAIL'}  {label}")
    return all_ok


def run_verified_cwd_smoke(port):
    """Run Get-Location via --cwd mode and check action + evidence."""
    label = "run-verified --cwd evidence"
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print("-" * 60)

    cwd = os.environ.get("CODEAPI_ROOT", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    rv = subprocess.run(
        MOD + ["-p", port, "--json", "term", "run-verified",
               "--cwd", cwd, "--run-timeout", "30", "Get-Location"],
        capture_output=True, text=True, timeout=180, cwd=PKG_PARENT)

    if rv.returncode != 0:
        print(f"FAIL  {label} — run-verified exited {rv.returncode}")
        print(rv.stdout)
        print(rv.stderr, file=sys.stderr)
        return False

    try:
        result = json.loads(rv.stdout)
    except json.JSONDecodeError:
        print(f"FAIL  {label} — output is not JSON")
        print(rv.stdout[:500])
        return False

    checks = []
    checks.append(("ok", result.get("ok") is True))
    checks.append(("action present",
                    result.get("action") in ("reused", "created")))
    comp = result.get("completion", {})
    checks.append(("start_marker_seen", comp.get("start_marker_seen") is True))
    checks.append(("end_marker_seen", comp.get("end_marker_seen") is True))
    st = result.get("status", {})
    checks.append(("exit_code=0", st.get("exit_code") == 0))
    before_cwd = (result.get("terminal_before") or {}).get("cwd")
    after_cwd = (result.get("terminal_after") or {}).get("cwd")
    checks.append(("cwd_preserved",
                    before_cwd is not None and before_cwd == after_cwd))

    all_ok = True
    for name, passed in checks:
        tag = "OK" if passed else "FAIL"
        print(f"  [{tag}] {name}")
        if not passed:
            all_ok = False
    action = result.get("action", "?")
    print(f"  action={action}")
    print(f"\n{'PASS' if all_ok else 'FAIL'}  {label}")

    if action == "created":
        tid = (result.get("terminal_before") or {}).get("id")
        if tid:
            subprocess.run(
                MOD + ["-p", port, "term", "kill", tid],
                capture_output=True, text=True, timeout=10, cwd=PKG_PARENT)

    return all_ok


def main():
    port = None
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = sys.argv[idx + 1]

    results = []

    # Offline: the package imports cleanly (replaces the seed's py_compile check).
    results.append(run("import codeapi_vscode package",
                       [sys.executable, "-c",
                        "import codeapi_vscode.cli, codeapi_vscode.client, "
                        "codeapi_vscode.terminal_client, codeapi_vscode.run_result"],
                       cwd=PKG_PARENT))

    results.append(run("codeapi --help", MOD + ["--help"], cwd=PKG_PARENT))
    results.append(run("codeapi term --help", MOD + ["term", "--help"], cwd=PKG_PARENT))
    results.append(run("codeapi sessions", MOD + ["sessions"], cwd=PKG_PARENT))

    if port:
        results.append(run("codeapi exec workspace name",
                           MOD + ["-p", port, "--json", "exec",
                                  "return vscode.workspace.name"], cwd=PKG_PARENT))

        results.append(run("codeapi term list",
                           MOD + ["-p", port, "--json", "term", "list"], cwd=PKG_PARENT))

        results.append(run("differential tests",
                           [sys.executable, DIFF_TEST, "--port", port]))

        results.append(run_verified_smoke(port))
        results.append(run_verified_cwd_smoke(port))
    else:
        print("\n--- Skipping live tests (no --port given) ---")

    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n{'='*60}")
    print(f"SMOKE: {passed}/{total} passed")
    print(f"{'='*60}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
