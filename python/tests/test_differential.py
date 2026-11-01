#!/usr/bin/env python3
"""Axis 1 — Differential tests: prove Python wrappers match raw /exec.

Migrated from legacy-seed/cli/test_differential.py (Phase 10). Logic is
unchanged; only the import moved from the flat `codeapi` module to the
`codeapi_vscode` package.

Run against a live VS Code window with CODEAPI active:

    python python/tests/test_differential.py [--port 3637]

Each test calls the operation two ways:
  1. Raw /exec with hand-written JavaScript (the old path)
  2. Python TermClient method (the new path)
Then compares. If they diverge, the Python abstraction changed behavior.
"""

import os
import sys
import traceback

# Make the package importable when run as a script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.client import CodeAPIClient
from codeapi_vscode.types import (TermInfo, TermCreateResult, TermWriteResult,
                                   TermReadResult, TermKillResult, _from_dict)


def diff(label, raw, typed):
    """Compare raw /exec result with typed Python result. Returns True if same."""
    if hasattr(typed, '__dataclass_fields__'):
        typed_dict = {k: getattr(typed, k) for k in typed.__dataclass_fields__}
    elif isinstance(typed, dict):
        typed_dict = typed
    else:
        typed_dict = typed

    if isinstance(raw, dict) and isinstance(typed_dict, dict):
        shared_keys = set(raw.keys()) & set(typed_dict.keys())
        mismatches = []
        for k in shared_keys:
            if raw[k] != typed_dict[k]:
                mismatches.append(f"  {k}: raw={raw[k]!r} vs typed={typed_dict[k]!r}")
        if mismatches:
            print(f"FAIL  {label}")
            for m in mismatches:
                print(m)
            return False
        raw_only = set(raw.keys()) - set(typed_dict.keys())
        if raw_only:
            print(f"WARN  {label} — raw has extra keys: {raw_only}")
        print(f"OK    {label}")
        return True

    if isinstance(raw, list) and isinstance(typed_dict, list):
        if len(raw) != len(typed_dict):
            print(f"FAIL  {label} — list length: raw={len(raw)} vs typed={len(typed_dict)}")
            return False
        print(f"OK    {label} — {len(raw)} items")
        return True

    if raw == typed_dict:
        print(f"OK    {label}")
        return True

    print(f"FAIL  {label} — raw={raw!r} vs typed={typed_dict!r}")
    return False


def run_tests(port=None):
    client = CodeAPIClient()
    kw = {"port": port} if port else {}

    results = []
    term_id = None
    typed_term_id = None

    try:
        print("\n--- term.list ---")
        raw_list = client.exec_js("return codeApiTerm.list()", **kw)
        typed_list = client.term.list(**kw)
        if isinstance(raw_list, list) and isinstance(typed_list, list):
            results.append(diff("list length", len(raw_list), len(typed_list)))
            if raw_list:
                results.append(diff("list[0]", raw_list[0], typed_list[0]))
        else:
            results.append(diff("list", raw_list, typed_list))

        print("\n--- term.create (shape) ---")
        raw_create = client.exec_js('return codeApiTerm.create({show:false})', **kw)
        typed_create = client.term.create(show=False, **kw)
        results.append(diff("create shape", set(raw_create.keys()),
                            set(typed_create.__dataclass_fields__.keys())
                            if hasattr(typed_create, '__dataclass_fields__')
                            else set(typed_create.keys())))

        term_id = raw_create.get("id") if isinstance(raw_create, dict) else None
        typed_term_id = typed_create.id if hasattr(typed_create, 'id') else None

        if not term_id:
            print("SKIP  no terminal id from create")
            return results

        print("\n--- term.write (plain) ---")
        raw_write = client.exec_js(
            f"return codeApiTerm.write('{term_id}', 'echo hello', {{enter:true}})", **kw)
        typed_write = client.term.write(term_id, "echo hello2", enter=True, **kw)
        results.append(diff("write.id", raw_write.get("id") if isinstance(raw_write, dict) else raw_write,
                            typed_write.id if isinstance(typed_write, TermWriteResult) else typed_write))
        results.append(diff("write shape", set(raw_write.keys()) if isinstance(raw_write, dict) else type(raw_write),
                            set(typed_write.__dataclass_fields__.keys())
                            if hasattr(typed_write, '__dataclass_fields__')
                            else type(typed_write)))

        import time
        time.sleep(0.5)

        print("\n--- term.read ---")
        raw_read = client.exec_js(
            f"return codeApiTerm.read('{term_id}', {{tail:2000}})", **kw)
        typed_read = client.term.read(term_id, tail=2000, **kw)
        if isinstance(raw_read, dict) and isinstance(typed_read, TermReadResult):
            results.append(diff("read.id", raw_read.get("id"), typed_read.id))
            raw_has = bool(raw_read.get("buffer"))
            typed_has = bool(typed_read.buffer)
            results.append(diff("read.buffer non-empty", raw_has, typed_has))
            results.append(diff("read shape", set(raw_read.keys()),
                                set(typed_read.__dataclass_fields__.keys())))
        else:
            results.append(diff("read", raw_read, typed_read))

        print("\n--- term.write (special chars via b64) ---")
        special = 'echo "hello & world"'
        import base64
        b64 = base64.b64encode(special.encode("utf-8")).decode("ascii")
        raw_write2 = client.exec_js(
            f"return codeApiTerm.write('{term_id}', "
            f"Buffer.from('{b64}','base64').toString(), {{enter:true}})", **kw)
        typed_write2 = client.term.write(term_id, special, enter=True, **kw)
        results.append(diff("write-special.id",
                            raw_write2.get("id") if isinstance(raw_write2, dict) else raw_write2,
                            typed_write2.id if isinstance(typed_write2, TermWriteResult) else typed_write2))
        results.append(diff("write-special shape",
                            set(raw_write2.keys()) if isinstance(raw_write2, dict) else type(raw_write2),
                            set(typed_write2.__dataclass_fields__.keys())
                            if hasattr(typed_write2, '__dataclass_fields__')
                            else type(typed_write2)))

        print("\n--- term.list (after creates) ---")
        raw_list2 = client.exec_js("return codeApiTerm.list()", **kw)
        typed_list2 = client.term.list(**kw)
        results.append(diff("list count after creates",
                            len(raw_list2) if isinstance(raw_list2, list) else raw_list2,
                            len(typed_list2) if isinstance(typed_list2, list) else typed_list2))

    except Exception as e:
        print(f"\nERROR: {e}")
        traceback.print_exc()
        results.append(False)

    finally:
        if term_id:
            try:
                client.exec_js(f"return codeApiTerm.kill('{term_id}')", **kw)
            except Exception:
                pass
        if typed_term_id and typed_term_id != term_id:
            try:
                client.term.kill(typed_term_id, **kw)
            except Exception:
                pass

    passed = sum(1 for r in results if r is True)
    failed = sum(1 for r in results if r is False)
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed, {len(results)} total")
    return results


if __name__ == "__main__":
    port = None
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])
    results = run_tests(port=port)
    sys.exit(1 if any(r is False for r in results) else 0)
