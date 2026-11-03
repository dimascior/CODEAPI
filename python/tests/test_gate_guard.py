#!/usr/bin/env python3
"""Gate guard unit tests — match, mismatch, expiry, replay, wrong terminal,
BOM, missing folder, unconfigured, and submit gate.

    python python/tests/test_gate_guard.py
"""

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.server import gate_guard  # noqa: E402


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_gate(directory, name, obj, bom=False):
    path = os.path.join(directory, name)
    raw = json.dumps(obj, separators=(",", ":"))
    with open(path, "wb") as fh:
        if bom:
            fh.write(b"\xef\xbb\xbf")
        fh.write(raw.encode("utf-8"))
    return path


def check(results, label, cond):
    results.append((label, bool(cond)))
    print("{}  {}".format("OK  " if cond else "FAIL", label))


def main():
    results = []

    # Reset consumed set between runs (module-level state).
    gate_guard._consumed.clear()

    # ── 1. Unconfigured (empty string) → pass ──
    ok, detail = gate_guard.verify_write_gate("", "t1", "hello", True)
    check(results, "unconfigured passes", ok and detail is None)

    # ── 2. Missing folder → fail closed ──
    ok, detail = gate_guard.verify_write_gate(
        os.path.join(tempfile.gettempdir(), "nonexistent_gate_dir_xyzzy"),
        "t1", "hello", True)
    check(results, "missing folder refuses", not ok and "does not exist" in detail)

    with tempfile.TemporaryDirectory() as tmpdir:
        inflight = os.path.join(tmpdir, "inflight")
        os.makedirs(inflight)

        # ── 3. Positive match ──
        _write_gate(inflight, "g1.gate.json", {
            "correlation_id": "corr-1",
            "data_sha256": _sha256("hello"),
            "terminal_id": "t1",
            "enter": True,
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "positive match returns True + correlation_id",
              ok and detail == "corr-1")

        # ── 4. Replay → refuse (same gate consumed) ──
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "replay of consumed gate refuses",
              not ok and "no admitted gate" in detail)

        # ── 5. Wrong data_sha256 ──
        _write_gate(inflight, "g2.gate.json", {
            "correlation_id": "corr-2",
            "data_sha256": _sha256("wrong"),
            "terminal_id": "t1",
            "enter": True,
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "wrong data_sha256 refuses", not ok)

        # ── 6. Wrong terminal_id ──
        _write_gate(inflight, "g3.gate.json", {
            "correlation_id": "corr-3",
            "data_sha256": _sha256("hello"),
            "terminal_id": "t-other",
            "enter": True,
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "wrong terminal_id refuses", not ok)

        # ── 7. Wrong enter ──
        _write_gate(inflight, "g4.gate.json", {
            "correlation_id": "corr-4",
            "data_sha256": _sha256("hello"),
            "terminal_id": "t1",
            "enter": False,
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "wrong enter refuses", not ok)

        # ── 8. Expired gate ──
        _write_gate(inflight, "g5.gate.json", {
            "correlation_id": "corr-5",
            "data_sha256": _sha256("hello"),
            "terminal_id": "t1",
            "enter": True,
            "expires_utc": "2020-01-01T00:00:00Z",
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "expired gate refuses", not ok)

        # ── 9. Unparseable expires_utc ──
        _write_gate(inflight, "g6.gate.json", {
            "correlation_id": "corr-6",
            "data_sha256": _sha256("hello"),
            "terminal_id": "t1",
            "enter": True,
            "expires_utc": "not-a-date",
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "unparseable expires_utc refuses", not ok)

        # ── 10. BOM in gate file ──
        _write_gate(inflight, "g7.gate.json", {
            "correlation_id": "corr-7",
            "data_sha256": _sha256("bom-data"),
            "terminal_id": "t1",
            "enter": True,
        }, bom=True)
        ok, detail = gate_guard.verify_write_gate(
            inflight, "t1", "bom-data", True)
        check(results, "BOM gate file parses and matches",
              ok and detail == "corr-7")

        # ── 11. Future expiry passes ──
        _write_gate(inflight, "g8.gate.json", {
            "correlation_id": "corr-8",
            "data_sha256": _sha256("future"),
            "terminal_id": "t1",
            "enter": True,
            "expires_utc": "2099-12-31T23:59:59Z",
        })
        ok, detail = gate_guard.verify_write_gate(
            inflight, "t1", "future", True)
        check(results, "future-expiry gate passes",
              ok and detail == "corr-8")

        # ── 12. Missing terminal_id in gate → refuses ──
        _write_gate(inflight, "g9.gate.json", {
            "correlation_id": "corr-9",
            "data_sha256": _sha256("hello"),
            "enter": True,
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "gate missing terminal_id refuses (fail closed)", not ok)

        # ── 13. Missing enter in gate → refuses ──
        _write_gate(inflight, "g10.gate.json", {
            "correlation_id": "corr-10",
            "data_sha256": _sha256("hello"),
            "terminal_id": "t1",
        })
        ok, detail = gate_guard.verify_write_gate(inflight, "t1", "hello", True)
        check(results, "gate missing enter refuses (fail closed)", not ok)

        # ── Submit gate tests ──
        gate_guard._consumed.clear()

        # ── 14. Submit match ──
        _write_gate(inflight, "s1.gate.json", {
            "correlation_id": "scorr-1",
            "submit_command_sha256": _sha256("git status"),
            "terminal_id": "t1",
        })
        ok, detail = gate_guard.verify_submit_gate(
            inflight, "t1", "git status")
        check(results, "submit positive match",
              ok and detail == "scorr-1")

        # ── 15. Submit replay ──
        ok, detail = gate_guard.verify_submit_gate(
            inflight, "t1", "git status")
        check(results, "submit replay refuses", not ok)

        # ── 16. Submit wrong command ──
        _write_gate(inflight, "s2.gate.json", {
            "correlation_id": "scorr-2",
            "submit_command_sha256": _sha256("git log"),
            "terminal_id": "t1",
        })
        ok, detail = gate_guard.verify_submit_gate(
            inflight, "t1", "git status")
        check(results, "submit wrong command refuses", not ok)

        # ── 17. Submit unconfigured passes ──
        ok, detail = gate_guard.verify_submit_gate("", "t1", "git status")
        check(results, "submit unconfigured passes", ok and detail is None)

    failed = [label for label, passed in results if not passed]
    print("\n{}/{} passed".format(len(results) - len(failed), len(results)))
    if failed:
        print("FAILED:")
        for f in failed:
            print("  - {}".format(f))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
