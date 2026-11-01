#!/usr/bin/env python3
"""discover_and_route — live window/session discovery + terminal routing demo.

Scans the legacy (3637-3646) + adapter (47337-47400) ranges, prints a classified
session table, selects a target window, and routes an adapter-owned terminal to it
(create -> echo marker -> read back -> exit -> observe alive:false/exitCode -> DELETE
-> read-after-delete 404). Liveness comes from /term/list, never from DELETE.

    set PYTHONPATH=python
    python python/tools/discover_and_route.py                 # auto-select adapter w/ terminals
    python python/tools/discover_and_route.py --port 47339
    python python/tools/discover_and_route.py --workspace CODEAPI --runtime-type adapter
    python python/tools/discover_and_route.py --no-route      # discovery only
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.discovery import (  # noqa: E402
    DEFAULT_RANGES, TerminalRouter, discover_sessions, select_by_identity,
    select_session)


def _short(v, n=8):
    return (v[:n] if isinstance(v, str) else "")


def print_table(sessions):
    print(f"discovered {len(sessions)} responder(s):")
    hdr = (f"  {'port':>5} {'type':<7} {'terms':<11} {'pyPid':>6} {'extPid':>6} "
           f"{'codeapiWindowId':<14} {'vscodeSess':<12} workspace")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for s in sorted(sessions, key=lambda x: x.port):
        print(f"  {s.port:>5} {s.runtime_type:<7} {s.terminal_routes:<11} "
              f"{str(s.pid or ''):>6} {str(s.extension_host_pid or ''):>6} "
              f"{_short(s.codeapi_window_id, 14):<14} {_short(s.vscode_session_id, 12):<12} "
              f"{s.workspace_uri or ''}")
        if s.flags:
            print(f"        flags: {', '.join(s.flags)}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="discover_and_route")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--session", dest="session_id", default=None)
    ap.add_argument("--runtime-type", default=None,
                    choices=["adapter", "legacy", "unknown"])
    ap.add_argument("--window", default=None,
                    help="select by identity token (codeapiWindowId > vscodeSessionId "
                         "> extensionHostPid > workspace > port)")
    ap.add_argument("--shell", default="powershell.exe")
    ap.add_argument("--no-route", action="store_true",
                    help="discovery only; do not create a terminal")
    args = ap.parse_args(argv)

    sessions = discover_sessions(DEFAULT_RANGES)
    print_table(sessions)
    if not sessions:
        print("\nNo CODEAPI runtimes found. Is a VS Code window with the extension open?")
        return 1

    dup_ws = sorted({s.workspace_uri for s in sessions
                     if s.workspace_uri and "duplicate-workspace" in s.flags})
    if dup_ws:
        print(f"\nwarning: {len(dup_ws)} workspace(s) open in multiple windows - "
              "workspace/port selection is AMBIGUOUS; pin --window <codeapiWindowId>.")

    if args.no_route:
        return 0

    # Selection: explicit criteria if given, else auto-pick an adapter runtime with
    # live terminal routes (the only kind that can actually own a terminal).
    try:
        if args.window:
            target = select_by_identity(sessions, args.window,
                                        require_terminals=not args.no_route)
        elif any([args.port, args.workspace, args.session_id, args.runtime_type]):
            target = select_session(
                sessions, port=args.port, workspace=args.workspace,
                session_id=args.session_id, runtime_type=args.runtime_type,
                require_terminals=not args.no_route)
        else:
            routable = [s for s in sessions
                        if s.runtime_type == "adapter" and s.terminals_usable]
            if len(routable) > 1:
                print(f"\nwarning: {len(routable)} adapter runtimes can own a terminal; "
                      "auto-selecting the first. Pin --window <codeapiWindowId> "
                      "to target a specific window.")
            target = select_session(sessions, runtime_type="adapter",
                                    require_terminals=True)
    except ValueError as e:
        print(f"\nselection failed: {e}")
        print("hint: only 'adapter' runtimes with terminal_routes=live can own a terminal.")
        return 2

    print(f"\nselected target: port={target.port} serverPort={target.server_port} "
          f"type={target.runtime_type}")
    print(f"  identity: codeapiWindowId={target.codeapi_window_id} "
          f"vscodeSessionId={target.vscode_session_id} "
          f"extensionHostPid={target.extension_host_pid} pyPid={target.pid} "
          f"workspace={target.workspace_uri}")

    router = TerminalRouter(target.base_url)
    res = router.run_and_reap(shell=args.shell)
    print(f"\nterminal routing (marker {res['marker']}):")
    for step, ok in res["steps"].items():
        print(f"  {'OK  ' if ok else 'FAIL'}  {step}")
    print(f"  terminal id = {res['terminal'].get('id')}, "
          f"pid = {res['terminal'].get('pid')}, exitCode = {res.get('exit_code')}")
    print(f"  dead record = {res.get('dead_record')}")
    print(f"\nROUTING {'PASS' if res['ok'] else 'FAIL'} "
          f"({sum(res['steps'].values())}/{len(res['steps'])})")
    return 0 if res["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
