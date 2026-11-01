"""codeapi_vscode.cli — argparse CLI, forward replacement for the Node CLI.

Ported from the retired seed CLI (Phase 10). Same command surface,
same flags, same output — the differential-equivalence contract (see
docs/PYTHON_PORT_ROADMAP.md Phase 10). Invoke as `python -m codeapi_vscode`.
"""

import argparse
import base64
import json
import sys

from .client import CodeAPIClient
from .errors import CodeAPIError
from .types import (SessionInfo, TermCreateResult, TermInfo, TermKillResult,
                    TermReadResult, TermWriteResult, _print, _term_id)


def _target_kw(args):
    """Extract targeting kwargs from parsed args."""
    return {"session_id": args.session, "workspace": args.workspace,
            "port": args.port, "runtime_id": getattr(args, "runtime_id", None)}


def cmd_sessions(client, args):
    sessions = client.discover()
    if args.json:
        out = []
        for s in sessions:
            info = s.info
            if isinstance(info, SessionInfo):
                out.append({"port": s.port, "info": {
                    "id": info.id, "pid": info.pid,
                    "serverPort": info.serverPort,
                    "workspaceUri": info.workspaceUri,
                    "windowId": info.windowId,
                    "capabilities": info.capabilities,
                    "codeapiWindowId": info.codeapiWindowId,
                    "vscodeSessionId": info.vscodeSessionId,
                    "extensionHostPid": info.extensionHostPid,
                }})
            else:
                out.append({"port": s.port, "info": info})
        print(json.dumps(out, indent=2))
        return
    if not sessions:
        print("No VS Code sessions found.")
        print("Make sure VS Code is running with the CODEAPI extension enabled.")
        return
    print(f"Found {len(sessions)} VS Code session(s):\n")
    for i, s in enumerate(sessions, 1):
        info = s.info
        if isinstance(info, SessionInfo):
            print(f"Session {i}:")
            print(f"  ID: {info.id}")
            print(f"  Port: {s.port}")
            print(f"  PID: {info.pid}")
            print(f"  Workspace: {info.workspaceUri or 'None'}")
            print(f"  Window ID: {info.codeapiWindowId or '(legacy/none)'}")
            print(f"  APIs: {len(info.capabilities)}")
        else:
            print(f"Session {i}:")
            print(f"  ID: {info.get('id')}")
            print(f"  Port: {s.port}")
            print(f"  PID: {info.get('pid')}")
            print(f"  Workspace: {info.get('workspaceUri') or 'None'}")
            print(f"  APIs: {len(info.get('capabilities') or [])}")
        print()


def cmd_exec(client, args):
    code = " ".join(args.code).strip()
    if not code:
        raise CodeAPIError("no code provided to exec")
    result = client.exec_js(code, **_target_kw(args))
    _print(result, as_json=args.json)


def cmd_exec_with_action(client, args):
    # NOTE: /exec-with-action is not served by the current extension runtime
    # (returns 404); it is standalone-only. Kept for parity with the seed CLI.
    result = client.exec_with_action(args.code, args.on_result, **_target_kw(args))
    if args.json or not isinstance(result, dict):
        _print(result, as_json=True)
    else:
        print("Result:", result.get("result", "No return value"))
        print("Action Result:", result.get("actionResult", "No action result"))


def cmd_command(client, args):
    result = client.command(args.command_id, args.args, **_target_kw(args))
    _print(result, as_json=True)


def cmd_apis(client, args):
    result = client.apis(**_target_kw(args))
    if args.json or not isinstance(result, list):
        _print(result, as_json=True)
        return
    print(f"Found {len(result)} available APIs:\n")
    grouped = {}
    for api in result:
        grouped.setdefault(api.get("category", "other"), []).append(api)
    for category, items in grouped.items():
        print(f"{category.upper()}:")
        for api in items:
            params = ", ".join(api.get("parameters", []))
            print(f"  {api.get('method')}({params})")
        print()


def cmd_message(client, args):
    client.message(args.text, args.type, **_target_kw(args))
    print("Message sent successfully!")


def cmd_health(client, args):
    _print(client.health(**_target_kw(args)), as_json=args.json)


def cmd_folders(client, args):
    _print(client.workspace_folders(**_target_kw(args)), as_json=args.json)


def cmd_editor(client, args):
    _print(client.editor_active(**_target_kw(args)), as_json=args.json)


def cmd_mesh(client, args):
    _print(client.mesh_peers(**_target_kw(args)), as_json=args.json)


def cmd_mesh_broadcast(client, args):
    try:
        body = json.loads(args.body) if args.body else {}
    except ValueError as e:
        raise CodeAPIError(f"body is not valid JSON: {e}") from e
    _print(client.mesh_broadcast(args.endpoint, body, **_target_kw(args)),
           as_json=args.json)


def cmd_endpoint_add(client, args):
    try:
        response = json.loads(args.response)
    except ValueError as e:
        raise CodeAPIError(f"response is not valid JSON: {e}") from e
    _print(client.endpoint_add(args.path, response, **_target_kw(args)),
           as_json=args.json)


def cmd_endpoint_remove(client, args):
    _print(client.endpoint_remove(args.path, **_target_kw(args)),
           as_json=args.json)


def cmd_term_list(client, args):
    result = client.term.list(**_target_kw(args))
    if args.json:
        if isinstance(result, list):
            _print([vars(r) if hasattr(r, '__dataclass_fields__') else r
                    for r in result], as_json=True)
        else:
            _print(result, as_json=True)
    else:
        if not result:
            print("No terminals in this window.")
            return
        for t in result:
            if isinstance(t, TermInfo):
                surface = t.surface or "unknown"
                if t.alive is True:
                    live = "alive"
                elif t.alive is False:
                    live = f"dead exit={t.exitCode}"
                else:
                    live = "state-unknown"
                print(f"  {t.id}  {surface:<15} {live:<20} "
                      f"pid={t.pid}  buffer={t.bytes} bytes")
            else:
                print(f"  {t}")


def cmd_term_create(client, args):
    result = client.term.create(
        id=getattr(args, 'id', None),
        cwd=getattr(args, 'cwd', None),
        shell=getattr(args, 'shell', None),
        show=getattr(args, 'show', False),
        **_target_kw(args))
    if args.json:
        _print(vars(result) if hasattr(result, '__dataclass_fields__') else result,
               as_json=True)
    else:
        if isinstance(result, TermCreateResult):
            visible = "yes" if result.surface == "vscode-terminal" else "no"
            print(f"Created terminal: {result.id}")
            print(f"  Shell:   {result.shell}")
            print(f"  CWD:     {result.cwd}")
            print(f"  PID:     {result.pid}")
            print(f"  Surface: {result.surface or 'unknown'}")
            print(f"  VS Code integrated terminal: {visible}")
        else:
            _print(result)


def cmd_term_write(client, args):
    tid = _term_id(args.id)
    enter = not args.no_enter
    if args.b64:
        b64 = args.text.strip()
        try:
            base64.b64decode(b64, validate=True)
        except ValueError as e:
            raise CodeAPIError(f"--b64 payload is not valid Base64: {e}") from e
        decoded = base64.b64decode(b64).decode("utf-8")
        result = client.term.write(tid, decoded, enter=enter, **_target_kw(args))
    else:
        result = client.term.write(tid, args.text, enter=enter, **_target_kw(args))
    if args.json:
        _print(vars(result) if hasattr(result, '__dataclass_fields__') else result,
               as_json=True)
    else:
        wrote = result.wrote if isinstance(result, TermWriteResult) else result
        print(f"wrote: {wrote}")


def cmd_term_read(client, args):
    result = client.term.read(
        _term_id(args.id), tail=args.tail, clear=args.clear,
        **_target_kw(args))
    if args.json:
        _print(vars(result) if hasattr(result, '__dataclass_fields__') else result,
               as_json=True)
    else:
        buf = result.buffer if isinstance(result, TermReadResult) else result
        if isinstance(buf, str):
            print(buf)
        else:
            _print(buf)


def cmd_term_resize(client, args):
    result = client.term.resize(
        _term_id(args.id), int(args.cols), int(args.rows),
        **_target_kw(args))
    _print(vars(result) if hasattr(result, '__dataclass_fields__') else result,
           as_json=True)


def cmd_term_kill(client, args):
    result = client.term.kill(_term_id(args.id), **_target_kw(args))
    if args.json:
        _print(vars(result) if hasattr(result, '__dataclass_fields__') else result,
               as_json=True)
    else:
        if isinstance(result, TermKillResult):
            print(f"{'Killed' if result.killed else 'Not found'}: {result.id}")
        else:
            _print(result)


def cmd_term_run(client, args):
    output = client.term.run(
        _term_id(args.id), args.command,
        timeout=args.run_timeout, poll_ms=args.poll_ms,
        settle_ms=args.settle_ms, settle=args.settle,
        wait_ms=args.wait_ms,
        **_target_kw(args))
    if args.json:
        print(json.dumps({"output": output}, indent=2))
    else:
        print(output)


def cmd_batch(client, args):
    try:
        with open(args.file, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f
                     if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError as e:
        raise CodeAPIError(f"Cannot read {args.file}: {e}") from e
    print(f"Executing {len(lines)} commands from {args.file}...\n")
    failures = 0
    kw = _target_kw(args)
    for i, line in enumerate(lines, 1):
        print(f"[{i}/{len(lines)}] {line}")
        try:
            if line.startswith("exec "):
                client.exec_js(line[5:], **kw)
            else:
                command_id, *cmd_args = line.split(" ")
                client.command(command_id, cmd_args, **kw)
            print("ok\n")
        except CodeAPIError as e:
            failures += 1
            print(f"error: {e}\n")
    print("Batch execution completed!" if not failures
          else f"Batch completed with {failures} failure(s).")
    if failures:
        sys.exit(1)


def cmd_verify(client, args):
    result = client.verify(**_target_kw(args))
    if args.json:
        _print(result, as_json=True)
        return
    for name, check in result["checks"].items():
        status = "OK" if check.get("ok") else "FAIL"
        detail_parts = [f"{k}={v}" for k, v in check.items() if k != "ok"]
        detail = "  " + ", ".join(detail_parts) if detail_parts else ""
        print(f"  [{status}] {name}{detail}")
    print()
    if result["ok"]:
        print("All checks passed.")
    else:
        print("Some checks failed.", file=sys.stderr)
        sys.exit(1)


def cmd_target(client, args):
    s = client._resolve(**_target_kw(args))
    info = s.info
    sid = info.id if isinstance(info, SessionInfo) else info.get("id", "")
    pid = info.pid if isinstance(info, SessionInfo) else info.get("pid", 0)
    ws = info.workspaceUri if isinstance(info, SessionInfo) else info.get("workspaceUri", "")
    cwid = info.codeapiWindowId if isinstance(info, SessionInfo) else info.get("codeapiWindowId")
    prefix = sid[:8]
    if args.json:
        _print({"id": sid, "prefix": prefix, "port": s.port,
                "pid": pid, "workspaceUri": ws,
                "codeapiWindowId": cwid,
                "target_flags": f"-s {prefix}"}, as_json=True)
    else:
        print(f"Session: {sid}")
        print(f"  Port:      {s.port}")
        print(f"  PID:       {pid}")
        print(f"  Workspace: {ws}")
        print(f"  Window ID: {cwid or '(legacy/none)'}")
        print(f"  Pin with:  -s {prefix}  or  -p {s.port}")


def cmd_term_inspect(client, args):
    info = client.term.inspect(_term_id(args.id), tail=args.tail,
                               **_target_kw(args))
    if args.json:
        _print(info, as_json=True)
    else:
        print(f"Terminal: {info['id']}")
        print(f"  Shell:  {info['shell']}")
        print(f"  CWD:    {info.get('cwd') or '(unknown)'}")
        print(f"  Idle:   {info['idle']}")
        print(f"  Buffer: {info.get('bytes', '?')} bytes")
        print(f"  Prompt: {info['prompt'][:120]}")


def cmd_term_find(client, args):
    results = client.term.find(
        cwd=getattr(args, 'cwd', None),
        shell=getattr(args, 'shell', None),
        idle_only=not getattr(args, 'all', False),
        **_target_kw(args))
    if args.json:
        _print(results, as_json=True)
    else:
        if not results:
            print("No matching terminals found.")
            return
        for info in results:
            idle_tag = "idle" if info["idle"] else "busy"
            print(f"  {info['id']}  {info['shell']}  "
                  f"{info.get('cwd') or '?'}  [{idle_tag}]")


def cmd_term_ensure(client, args):
    result = client.term.ensure(
        cwd=args.cwd,
        shell=getattr(args, 'shell', None),
        show=True,
        **_target_kw(args))
    if args.json:
        _print(result, as_json=True)
    else:
        action = result["action"]
        t = result["terminal"]
        tid = t.get("id", "?")
        print(f"{action.capitalize()}: {tid}  (verified={result.get('verified')})")
        if t.get("cwd"):
            print(f"  CWD:     {t['cwd']}")
        if t.get("shell"):
            print(f"  Shell:   {t['shell']}")
        print(f"  Surface: {t.get('surface') or 'unknown'}")
        print(f"  Alive:   {t.get('alive')}")


def cmd_operations(client, args):
    op_id = getattr(args, "operation_id", None)
    result = client.operations(operation_id=op_id, **_target_kw(args))
    if args.json or op_id:
        _print(result, as_json=True)
        return
    if isinstance(result, dict) and isinstance(result.get("operations"), list):
        print(f"protocol: {result.get('protocol')}")
        print(f"operations ({len(result['operations'])}):")
        for op in result["operations"]:
            print(f"  {op.get('id'):<26} {op.get('purpose', '')}")
        print("\nInspect one:  codeapi operations <id>   "
              "(e.g. terminal.open_visible, terminal.run_verified)")
    else:
        _print(result, as_json=True)


def cmd_term_open_visible(client, args):
    tk = _target_kw(args)
    result = client.term.open_visible(
        runtime_id=tk.get("runtime_id"), port=tk.get("port"),
        shell=(getattr(args, "shell", None) or "powershell.exe"),
        cwd=getattr(args, "cwd", None), term_id=getattr(args, "id", None))
    if args.json:
        _print(result, as_json=True)
        if result.get("status") != "verified":
            sys.exit(1)
        return
    status = result.get("status")
    if status == "verified":
        tgt = result.get("target", {})
        term = result.get("terminal", {})
        print("[verified] visible CODEAPI terminal open")
        print(f"  Runtime:   {tgt.get('codeapiRuntimeId')}")
        print(f"  Workspace: {tgt.get('workspaceUri')}")
        print(f"  Port:      {tgt.get('serverPort')}")
        print(f"  Terminal:  {term.get('id')}  pid={term.get('pid')}")
        print(f"  Shell:     {term.get('shell')}")
        print(f"  Surface:   {term.get('surface')}")
        print(f"  Alive:     {term.get('alive')}  exitCode={term.get('exitCode')}")
        print(f"  {result.get('claim', '')}")
    else:
        print(f"[{status}] {result.get('error')}", file=sys.stderr)
        for c in result.get("candidates", []) or []:
            print(f"    runtimeId={c.get('codeapiRuntimeId')} "
                  f"workspace={c.get('workspaceUri')} port={c.get('serverPort')}",
                  file=sys.stderr)
        sys.exit(1)


def _observe_state(client, tid, tk):
    """A fresh /term/list observation for one id:
    (listed, surface, alive, exitCode, pid). Missing id -> (False, None, ...)."""
    try:
        for t in client.term.list(**tk):
            if (t.id if isinstance(t, TermInfo) else t.get("id")) == tid:
                if isinstance(t, TermInfo):
                    return True, t.surface, t.alive, t.exitCode, t.pid
                return (True, t.get("surface"), t.get("alive"),
                        t.get("exitCode"), t.get("pid"))
    except CodeAPIError:
        pass
    return False, None, None, None, None


def _rv_fail_preflight(args, pf):
    """Emit a run_verified precondition failure and exit 1 WITHOUT executing.
    Both the id-based and --cwd paths share this gate: a command never runs
    against a terminal that is missing, headless, dead, or exited."""
    t = pf.get("terminal", {}) or {}
    msg = pf.get("error") or (
        f"precondition failed [{pf['code']}]: terminal {t.get('id')} "
        f"listed={pf.get('listed')} surface={t.get('surface')} "
        f"alive={t.get('alive')} exitCode={t.get('exitCode')}")
    out = {"operation": "terminal.run_verified", "ok": False,
           "code": pf.get("code"), "error": msg,
           "listed": pf.get("listed", False), "terminal": t, "executed": False}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    else:
        print(f"[FAIL] {msg}", file=sys.stderr)
    sys.exit(1)


def cmd_term_run_verified(client, args):
    cwd = getattr(args, "cwd", None)
    positionals = args.rv_args
    action = None
    tk = _target_kw(args)

    # 1. Resolve the terminal id (both modes) --------------------------------
    if cwd:
        command = " ".join(positionals)
        if not command:
            raise CodeAPIError("--cwd mode requires a command")
        ensure_result = client.term.ensure(
            cwd=cwd, shell=args.shell, show=True, **tk)
        ensure_terminal = ensure_result.get("terminal") or {}
        tid = ensure_terminal.get("id")
        action = ensure_result.get("action")
        if not tid:
            _rv_fail_preflight(args, {"code": "CREATE_FAILED", "listed": False,
                                      "terminal": {"id": None},
                                      "error": "ensure did not yield a terminal id"})
    else:
        if len(positionals) < 2:
            raise CodeAPIError(
                "id-based mode requires <id> <command>. "
                "Use --cwd for automatic terminal selection.")
        tid = _term_id(positionals[0])
        command = " ".join(positionals[1:])

    # 2. SHARED preflight — an id SELECTS a resource, it does not bypass the
    #    resource's state contract. Both modes require a live VISIBLE terminal.
    pf = client.term.preflight_visible(tid, **tk)
    if not pf["ok"]:
        _rv_fail_preflight(args, pf)
    pre_term = pf["terminal"]           # full registry record (carries pid)

    # 3. Execute -------------------------------------------------------------
    result = client.term.run_result(
        tid, command, shell=args.shell,
        timeout=args.run_timeout, poll_ms=args.poll_ms, **tk)

    # 4. Post-run OBSERVED terminal state (before any deliberate teardown) ----
    listed, surface, alive, exit_code, pid = _observe_state(client, tid, tk)
    if pid is None:
        pid = pre_term.get("pid")       # registry pid survives id-based mode

    dispatch = result.dispatch or {}
    completion = result.completion or {}
    status = result.status or {}
    session = result.session or {}
    wrote = dispatch.get("bytes_written", 0)
    output_present = bool(result.output)
    completion_observed = bool(completion.get("end_marker_seen"))
    command_exit = status.get("exit_code")

    # ok = command COMPLETED (start+end markers, parsed exit 0) in a still-live
    # VISIBLE terminal. Non-empty OUTPUT is NOT required — a valid command may
    # legitimately print nothing. Completion and user-output are different facts.
    ok = bool(
        result.ok
        and completion_observed
        and listed
        and surface == "vscode-terminal"
        and alive is True
        and exit_code is None)

    # 5. Optional teardown (explicit request, NOT a success condition) --------
    cleanup_requested = bool(getattr(args, "reap", False))
    reaped = False
    if cleanup_requested:
        try:
            kill_res = client.term.kill(tid, **tk)
            reaped = bool(kill_res.killed if isinstance(kill_res, TermKillResult)
                          else (kill_res or {}).get("killed", False))
        except CodeAPIError:
            reaped = False

    # 6. leftOpen is an OBSERVED final state — "not deleted" is not "open".
    #    A terminal that died naturally (or was reaped) is NOT left open.
    f_listed, f_surface, f_alive, _fe, _fp = _observe_state(client, tid, tk)
    left_open = bool(f_listed and f_surface == "vscode-terminal" and f_alive is True)

    out = {
        "operation": "terminal.run_verified",
        "ok": ok,
        "mode": "cwd" if cwd else "id",
        "executed": True,
        "target": {"codeapiRuntimeId": session.get("codeapiRuntimeId"),
                   "workspaceUri": session.get("workspace_uri"),
                   "serverPort": session.get("port")},
        "terminal": {"id": tid, "pid": pid,
                     "surface": surface, "alive": alive, "exitCode": exit_code,
                     "listed": listed, "leftOpen": left_open},
        "created": (action == "created") if action else False,
        "listed": listed,
        "inputAccepted": wrote > 0,
        "outputObserved": output_present,
        "completionObserved": completion_observed,
        "commandExitCode": command_exit,
        "exitObserved": exit_code is not None,
        "cleanupRequested": cleanup_requested,
        "reaped": reaped,
        "leftOpen": left_open,
        "dispatch": {"wrote": wrote, "meaning": "input accepted"},
        "output": {"bytes": len(result.output or ""), "clean": result.output},
        "completion": {"markerObserved": completion_observed,
                       "commandExitCode": command_exit,
                       "elapsed_ms": completion.get("elapsed_ms")},
        "warnings": result.warnings,
    }

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        t = out["terminal"]
        print("Target")
        print(f"  Runtime:   {out['target'].get('codeapiRuntimeId')}")
        print(f"  Workspace: {out['target'].get('workspaceUri')}")
        print(f"  Port:      {out['target'].get('serverPort')}")
        print("Terminal")
        print(f"  ID:        {t['id']}")
        print(f"  PID:       {t.get('pid')}")
        print(f"  Surface:   {t.get('surface') or 'unknown'}")
        print(f"  Listed:    {'yes' if out['listed'] else 'no'}")
        print(f"  Alive:     {'yes' if t.get('alive') is True else t.get('alive')}")
        print(f"  Exit code: {'running' if t.get('exitCode') is None else t.get('exitCode')}")
        print(f"  Left open: {'yes' if left_open else 'no'}"
              f"{' (reaped)' if reaped else ''}")
        print("Command")
        print(f"  Input accepted:    {wrote} bytes")
        print(f"  Command exit code: "
              f"{'?' if command_exit is None else command_exit}")
        print(f"  Completion marker: {'yes' if completion_observed else 'no'}")
        print(f"  Output observed:   {'yes' if output_present else 'no'} "
              "(empty output can still be success)")
        print("Output")
        print(result.output or "  (no output body)")
        for w in result.warnings:
            print(f"  warning: {w}", file=sys.stderr)
    if not ok:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Argparse setup
# ---------------------------------------------------------------------------

COMMON_DEFAULTS = {"session": None, "workspace": None, "port": None,
                   "runtime_id": None, "json": False, "verbose": False,
                   "timeout": 30.0}


def build_parser():
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("-s", "--session", help="Target session by id prefix")
    common.add_argument("-w", "--workspace", help="Target session by workspace path")
    common.add_argument("-p", "--port", type=int,
                        help="Target session by exact port (stable across id rotation)")
    common.add_argument("-r", "--runtime-id", dest="runtime_id",
                        help="Target the unique live runtime by codeapiRuntimeId "
                             "(strict; fails on ambiguity)")
    common.add_argument("-j", "--json", action="store_true", help="JSON output")
    common.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    common.add_argument("--timeout", type=float,
                        help="Request timeout in seconds (default 30)")

    parser = argparse.ArgumentParser(
        prog="codeapi", parents=[common],
        description="CLI for the CODEAPI VS Code control bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sessions", aliases=["ls"], parents=[common],
                       help="List available VS Code sessions")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("exec", parents=[common],
                       help="Execute JavaScript in the extension host")
    p.add_argument("code", nargs="+", help="JavaScript source")
    p.set_defaults(func=cmd_exec)

    # Help string kept byte-identical to the seed CLI so the axis-2 parity
    # oracle stays exception-free (see the handler comment / client docstring /
    # python/README.md for the "standalone-only, 404" warning). The --help
    # annotation returns at Phase 15 when the seed retires and parity is decommissioned.
    p = sub.add_parser("exec-with-action", parents=[common],
                       help="Execute JavaScript, then a follow-up action")
    p.add_argument("code")
    p.add_argument("on_result")
    p.set_defaults(func=cmd_exec_with_action)

    p = sub.add_parser("command", aliases=["cmd"], parents=[common],
                       help="Execute a VS Code command")
    p.add_argument("command_id")
    p.add_argument("args", nargs="*", default=[])
    p.set_defaults(func=cmd_command)

    p = sub.add_parser("apis", parents=[common], help="List available VS Code APIs")
    p.set_defaults(func=cmd_apis)

    p = sub.add_parser("message", aliases=["msg"], parents=[common],
                       help="Show a message in VS Code")
    p.add_argument("text")
    p.add_argument("-t", "--type", choices=["info", "warning", "error"], default="info")
    p.set_defaults(func=cmd_message)

    p = sub.add_parser("batch", parents=[common],
                       help="Execute commands from a file (one per line)")
    p.add_argument("file")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("verify", parents=[common],
                       help="Golden-path preflight check")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("target", parents=[common],
                       help="Resolve and display session targeting info")
    p.set_defaults(func=cmd_target)

    p = sub.add_parser("health", parents=[common], help="GET /health")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("folders", parents=[common], help="GET /workspace/folders")
    p.set_defaults(func=cmd_folders)

    p = sub.add_parser("editor", parents=[common], help="GET /editor/active")
    p.set_defaults(func=cmd_editor)

    p = sub.add_parser("mesh", parents=[common], help="GET /mesh/peers")
    p.set_defaults(func=cmd_mesh)

    p = sub.add_parser("mesh-broadcast", parents=[common],
                       help="POST /mesh/broadcast/<endpoint>")
    p.add_argument("endpoint")
    p.add_argument("body", nargs="?", help="JSON request body (default {})")
    p.set_defaults(func=cmd_mesh_broadcast)

    p = sub.add_parser("endpoint-add", parents=[common],
                       help="Register a dynamic canned-response endpoint")
    p.add_argument("path", help="Endpoint path starting with /")
    p.add_argument("response", help="JSON payload the endpoint will return")
    p.set_defaults(func=cmd_endpoint_add)

    p = sub.add_parser("endpoint-remove", parents=[common],
                       help="Remove a dynamic endpoint")
    p.add_argument("path")
    p.set_defaults(func=cmd_endpoint_remove)

    p = sub.add_parser("operations", aliases=["ops"], parents=[common],
                       help="Machine-readable CODEAPI operation digest (GET /operations)")
    p.add_argument("operation_id", nargs="?",
                   help="Show one operation (e.g. terminal.open_visible)")
    p.set_defaults(func=cmd_operations)

    p = sub.add_parser("term", parents=[common],
                       help="Drive VS Code terminals via the public /term/* routes")
    tsub = p.add_subparsers(dest="term_cmd", required=True)

    t = tsub.add_parser("open-visible", parents=[common],
                        help="Create + verify a visible VS Code terminal (composite)")
    t.add_argument("--cwd")
    t.add_argument("--shell")
    t.add_argument("--id")
    t.set_defaults(func=cmd_term_open_visible)

    t = tsub.add_parser("list", parents=[common], help="List PTYs")
    t.set_defaults(func=cmd_term_list)

    t = tsub.add_parser("create", parents=[common], help="Create a PTY")
    t.add_argument("--cwd")
    t.add_argument("--shell")
    t.add_argument("--id")
    t.add_argument("--show", action="store_true", help="Reveal in VS Code")
    t.set_defaults(func=cmd_term_create)

    t = tsub.add_parser("write", parents=[common], help="Type into a PTY")
    t.add_argument("id")
    t.add_argument("text")
    t.add_argument("--no-enter", action="store_true")
    t.add_argument("--b64", action="store_true",
                   help="TEXT is pre-encoded Base64")
    t.set_defaults(func=cmd_term_write)

    t = tsub.add_parser("read", parents=[common], help="Read PTY buffer")
    t.add_argument("id")
    t.add_argument("--tail", type=int, default=4000)
    t.add_argument("--clear", action="store_true")
    t.set_defaults(func=cmd_term_read)

    t = tsub.add_parser("resize", parents=[common], help="Resize a PTY")
    t.add_argument("id")
    t.add_argument("cols", type=int)
    t.add_argument("rows", type=int)
    t.set_defaults(func=cmd_term_resize)

    t = tsub.add_parser("kill", parents=[common], help="Kill a PTY")
    t.add_argument("id")
    t.set_defaults(func=cmd_term_kill)

    t = tsub.add_parser("run", parents=[common],
                        help="Run a command and wait for output")
    t.add_argument("id", help="Terminal id")
    t.add_argument("command", help="Command to execute in the PTY")
    t.add_argument("--run-timeout", type=int, default=120, dest="run_timeout",
                   help="Hard timeout in seconds (default 120)")
    t.add_argument("--poll-ms", type=int, default=200,
                   help="Poll interval in ms (default 200)")
    t.add_argument("--settle-ms", type=int, default=800,
                   help="Quiet period for --settle mode (default 800)")
    t.add_argument("--settle", action="store_true",
                   help="Wait for output to stop changing instead of sentinel")
    t.add_argument("--wait-ms", type=int, default=0,
                   help="Fixed delay snapshot (>0 enables)")
    t.set_defaults(func=cmd_term_run)

    t = tsub.add_parser("inspect", parents=[common],
                        help="Analyze a PTY's shell, cwd, and idle state")
    t.add_argument("id")
    t.add_argument("--tail", type=int, default=4000)
    t.set_defaults(func=cmd_term_inspect)

    t = tsub.add_parser("find", parents=[common],
                        help="Search for terminals matching criteria")
    t.add_argument("--cwd", help="Match terminals whose cwd contains this path")
    t.add_argument("--shell", help="Match terminals running this shell type")
    t.add_argument("--all", action="store_true",
                   help="Include busy (non-idle) terminals")
    t.set_defaults(func=cmd_term_find)

    t = tsub.add_parser("ensure", parents=[common],
                        help="Reuse a matching idle PTY or create one")
    t.add_argument("--cwd", required=True,
                   help="Desired working directory")
    t.add_argument("--shell", help="Desired shell (e.g. powershell, cmd)")
    t.set_defaults(func=cmd_term_ensure)

    t = tsub.add_parser("run-verified", parents=[common],
                        help="Run a command with full evidence capture")
    t.add_argument("rv_args", nargs="+", metavar="ARG",
                   help="<id> <command> or just <command> with --cwd")
    t.add_argument("--cwd",
                   help="Select terminal by cwd (calls ensure internally)")
    t.add_argument("--shell", choices=["powershell", "cmd"],
                   default="powershell",
                   help="Shell wrapper type (default powershell)")
    t.add_argument("--run-timeout", type=int, default=120,
                   dest="run_timeout",
                   help="Hard timeout in seconds (default 120)")
    t.add_argument("--poll-ms", type=int, default=200,
                   help="Poll interval in ms (default 200)")
    t.add_argument("--reap", action="store_true",
                   help="Delete the terminal after the run (default: leave it open)")
    t.set_defaults(func=cmd_term_run_verified)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    for key, value in COMMON_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    client = CodeAPIClient(timeout=args.timeout)
    try:
        args.func(client, args)
    except CodeAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
