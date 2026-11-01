#!/usr/bin/env python3
"""Terminal corridor acceptance — native /term/* wiring, typed field preservation,
the /operations digest, and the open_visible / run_verified composites. No VS Code.

  python python/tests/test_terminal_corridor.py

A FakeClient records (method, path, body) for every HTTP call so we can prove the
TermClient hits the native public routes (never /exec / codeApiTerm). The digest route
runs against the REAL server with a fake adapter. Discovery is monkeypatched for the
composite tests.
"""

import io
import json
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode import cli, discovery                       # noqa: E402
from codeapi_vscode.client import CodeAPIClient                 # noqa: E402
from codeapi_vscode.discovery import DiscoveredSession          # noqa: E402
from codeapi_vscode.errors import CodeAPIError                  # noqa: E402
from codeapi_vscode.terminal_client import TermClient           # noqa: E402
from codeapi_vscode.types import (TermCreateResult, TermInfo,   # noqa: E402
                                  _from_dict)
from codeapi_vscode.runtime import create_server                # noqa: E402
from codeapi_vscode.session.port_allocator import PortAllocator  # noqa: E402


RESULTS = []


def check(label, cond):
    RESULTS.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}")


class FakeClient:
    """Records every HTTP call; returns canned per-path payloads."""

    def __init__(self, list_data=None, create_data=None, read_data=None):
        self.calls = []
        self._list = list_data if list_data is not None else []
        self._create = create_data if create_data is not None else {
            "id": "tNEW", "shell": "powershell.exe", "cwd": "C:/x", "pid": 5,
            "surface": "vscode-terminal"}
        self._read = read_data if read_data is not None else {
            "id": "t1", "bytes": 3, "buffer": "hi\n"}
        self.term = TermClient(self)

    def get(self, path, **tk):
        self.calls.append(("GET", path, None))
        if path.startswith("/term/list"):
            return self._list
        if "/read" in path:
            return self._read
        return None

    def post(self, path, body, **tk):
        self.calls.append(("POST", path, body))
        if path == "/term/create":
            return self._create
        if path.endswith("/write"):
            return {"id": path.split("/")[2], "wrote": len((body or {}).get("data", ""))}
        if path.endswith("/resize"):
            return {"id": path.split("/")[2], "cols": body["cols"], "rows": body["rows"]}
        return None

    def delete(self, path, **tk):
        self.calls.append(("DELETE", path, None))
        tid = path.split("/")[2]
        # a real DELETE removes the terminal from the registry, so a later
        # /term/list no longer lists it — leftOpen is OBSERVED, not assumed.
        self._list = [e for e in self._list
                      if (e.get("id") if isinstance(e, dict)
                          else getattr(e, "id", None)) != tid]
        return {"id": tid, "killed": True}

    def exec_js(self, code, **tk):
        self.calls.append(("EXEC", code, None))
        return None

    def _resolve(self, **tk):
        info = types.SimpleNamespace(id="sid", pid=1, workspaceUri="file:///repo",
                                     codeapiRuntimeId="RID-1")
        return types.SimpleNamespace(info=info, base_url="http://127.0.0.1:47337",
                                     port=47337)

    def _http(self, method, url, **kw):
        self.calls.append((method, url, kw.get("body")))
        return None


def _ns(**kw):
    base = {"session": None, "workspace": None, "port": None, "runtime_id": None,
            "json": False, "verbose": False, "timeout": 30.0}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _capture(fn):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _capture_exit(fn):
    """Run fn, capturing stdout AND swallowing SystemExit; returns (text, code)."""
    buf = io.StringIO()
    code = None
    with redirect_stdout(buf):
        try:
            fn()
        except SystemExit as e:
            code = e.code
    return buf.getvalue(), code


def _ds(port, rid, wsid="WS-1", ws="file:///repo", sid=None):
    # raw_session mirrors what discovery captures from GET /session — target()
    # rebuilds a SessionInfo from it, whose id/pid/serverPort are required.
    raw = {"id": sid or f"sid-{port}", "pid": 1000 + port, "serverPort": port,
           "workspaceUri": ws, "codeapiRuntimeId": rid, "codeapiWorkspaceId": wsid}
    return DiscoveredSession(
        port=port, base_url=f"http://127.0.0.1:{port}", healthy=True,
        server_port=port, workspace_uri=ws, session_id=sid, codeapi_runtime_id=rid,
        codeapi_workspace_id=wsid, terminal_routes="live", runtime_type="adapter",
        raw_session=raw)


# --- 1-7: TermClient uses the native /term/* routes, never /exec ---------------

def native_route_tests():
    fc = FakeClient()
    fc.term.list()
    check("1. list() -> GET /term/list", fc.calls[-1][:2] == ("GET", "/term/list"))
    fc.term.create(cwd="C:/x", show=True)
    check("2. create() -> POST /term/create", fc.calls[-1][:2] == ("POST", "/term/create"))
    fc.term.write("t1", "echo hi")
    check("3. write() -> POST /term/:id/write", fc.calls[-1][:2] == ("POST", "/term/t1/write"))
    fc.term.read("t1")
    check("4. read() -> GET /term/:id/read", fc.calls[-1][1].startswith("/term/t1/read"))
    fc.term.resize("t1", 80, 24)
    check("5. resize() -> POST /term/:id/resize", fc.calls[-1][:2] == ("POST", "/term/t1/resize"))
    fc.term.kill("t1")
    check("6. kill() -> DELETE /term/:id", fc.calls[-1][:2] == ("DELETE", "/term/t1"))
    joined = " ".join(str(c) for c in fc.calls)
    check("7. no codeApiTerm/exec in terminal ops",
          "EXEC" not in [c[0] for c in fc.calls] and "codeApiTerm" not in joined)


# --- 8-9: typed field preservation --------------------------------------------

def typed_field_tests():
    ti = _from_dict(TermInfo, {"id": "t1", "pid": 9, "bytes": 4,
                               "alive": False, "exitCode": -1073741510,
                               "surface": "process"})
    check("8. TermInfo preserves surface/alive/exitCode",
          ti.surface == "process" and ti.alive is False and ti.exitCode == -1073741510)
    check("8b. TermInfo.is_visible_terminal false for dead process",
          ti.is_visible_terminal is False)
    live = _from_dict(TermInfo, {"id": "t2", "pid": 1, "bytes": 0,
                                 "alive": True, "surface": "vscode-terminal"})
    check("8c. is_visible_terminal true for alive vscode-terminal",
          live.is_visible_terminal is True)
    legacy = _from_dict(TermInfo, {"id": "t3", "pid": 1, "bytes": 0})
    check("8d. absent surface -> unknown, not visible",
          legacy.surface is None and legacy.is_visible_terminal is False)
    cr = _from_dict(TermCreateResult, {"id": "t1", "shell": "p", "cwd": "c",
                                       "pid": 1, "surface": "vscode-terminal"})
    check("9. TermCreateResult preserves surface", cr.surface == "vscode-terminal")
    infos = FakeClient(list_data=[{"id": "t9", "pid": 3, "bytes": 7, "alive": False,
                                   "exitCode": 5, "surface": "process"}]).term.list()
    check("9b. TermClient.list carries surface+alive+exitCode together",
          isinstance(infos[0], TermInfo) and infos[0].surface == "process"
          and infos[0].alive is False and infos[0].exitCode == 5)


# --- 10-12: CLI formatting -----------------------------------------------------

def cli_format_tests():
    fc = FakeClient(list_data=[
        {"id": "tA", "pid": 1, "bytes": 5, "alive": True, "exitCode": None,
         "surface": "vscode-terminal"},
        {"id": "tB", "pid": 2, "bytes": 9, "alive": True, "exitCode": None,
         "surface": "process"}])
    out = _capture(lambda: cli.cmd_term_list(fc, _ns(json=True)))
    parsed = json.loads(out)
    fields_ok = all(set(e) >= {"id", "pid", "bytes", "alive", "exitCode", "surface"}
                    for e in parsed)
    check("10. term list --json emits all native fields", fields_ok)

    fc2 = FakeClient()
    out = _capture(lambda: cli.cmd_term_create(fc2, _ns(json=True, cwd="C:/x",
                                                        shell=None, id=None, show=True)))
    check("11. term create --json emits surface",
          json.loads(out).get("surface") == "vscode-terminal")

    out = _capture(lambda: cli.cmd_term_list(fc, _ns(json=False)))
    check("12. human list distinguishes process vs vscode-terminal",
          "vscode-terminal" in out and "process" in out and "alive" in out)


# --- 13-14: ensure gating (surface + liveness) --------------------------------

def ensure_tests():
    prompt = "PS C:\\repo> "
    # a matching, idle, but surface:process terminal -> must NOT be reused for show=True
    fc = FakeClient(
        list_data=[{"id": "t1", "pid": 1, "bytes": 10, "alive": True,
                    "exitCode": None, "surface": "process"}],
        read_data={"id": "t1", "bytes": 12, "buffer": prompt})
    res = fc.term.ensure(cwd="C:\\repo", shell="powershell", show=True)
    check("13. ensure(show=True) does not reuse surface:process",
          res["action"] == "created")

    fc = FakeClient(
        list_data=[{"id": "t1", "pid": 1, "bytes": 10, "alive": False,
                    "exitCode": 0, "surface": "vscode-terminal"}],
        read_data={"id": "t1", "bytes": 12, "buffer": prompt})
    res = fc.term.ensure(cwd="C:\\repo", shell="powershell", show=True)
    check("14. ensure(show=True) skips dead terminal", res["action"] == "created")


# --- 15-20: open_visible composite + strict targeting -------------------------

def open_visible_tests():
    orig = discovery.discover_sessions
    try:
        discovery.discover_sessions = lambda **k: [_ds(47337, "RID-1")]

        fc = FakeClient(
            create_data={"id": "tX", "shell": "powershell.exe", "cwd": "C:/x",
                         "pid": 9, "surface": "vscode-terminal"},
            list_data=[{"id": "tX", "pid": 9, "bytes": 0, "alive": True,
                        "exitCode": None, "surface": "vscode-terminal"}])
        r = fc.term.open_visible(runtime_id="RID-1")
        check("15. open_visible verifies the exact created id",
              r["status"] == "verified" and r["terminal"]["id"] == "tX")
        check("19. open_visible returns selected runtime identity",
              r["target"]["codeapiRuntimeId"] == "RID-1")

        fc = FakeClient(create_data={"id": "tX", "shell": "p", "cwd": "c", "pid": 9,
                                     "surface": "process"})
        r = fc.term.open_visible(runtime_id="RID-1")
        check("16. open_visible fails on surface:process",
              r["status"] == "SURFACE_MISMATCH")

        fc = FakeClient(
            create_data={"id": "tX", "shell": "p", "cwd": "c", "pid": 9,
                         "surface": "vscode-terminal"}, list_data=[])
        r = fc.term.open_visible(runtime_id="RID-1")
        check("17. open_visible fails when id absent from /term/list",
              r["status"] == "TERMINAL_NOT_LISTED")

        fc = FakeClient(
            create_data={"id": "tX", "shell": "p", "cwd": "c", "pid": 9,
                         "surface": "vscode-terminal"},
            list_data=[{"id": "tX", "pid": 9, "bytes": 0, "alive": False,
                        "exitCode": 1, "surface": "vscode-terminal"}])
        r = fc.term.open_visible(runtime_id="RID-1")
        check("18. open_visible fails when alive:false", r["status"] == "TERMINAL_DEAD")

        # ambiguous: two adapters, no runtime_id -> fail, do not pick first
        discovery.discover_sessions = lambda **k: [_ds(47337, "RID-1"),
                                                   _ds(47338, "RID-2")]
        fc = FakeClient()
        r = fc.term.open_visible()
        check("20. ambiguous selection fails (not first-match)",
              r["status"] == "TARGET_AMBIGUOUS" and len(r.get("candidates", [])) == 2
              and not any(c[1] == "/term/create" for c in fc.calls))
    finally:
        discovery.discover_sessions = orig


# --- 21-24: /operations digest (real server + fake adapter) -------------------

class FakeAdapter:
    def apis(self):
        return [{"method": f"api{i}", "category": "x"} for i in range(13)]

    def term_list(self):
        return []


def _raw(base, method, path):
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "null")


def _wait(base):
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)


def digest_tests():
    port = PortAllocator().resolve(None)
    server, _ = create_server("127.0.0.1", port, adapter=FakeAdapter())
    threading.Thread(target=server.start, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    _wait(base)
    try:
        st, body = _raw(base, "GET", "/operations")
        ids = [o.get("id") for o in body.get("operations", [])] if isinstance(body, dict) else []
        check("21. GET /operations -> versioned digest",
              st == 200 and body.get("protocol") == "codeapi.operations.v1"
              and "terminal.open_visible" in ids and "terminal.run_verified" in ids)

        st, ov = _raw(base, "GET", "/operations/terminal.open_visible")
        require = (ov.get("verify", {}).get("require", {}) if isinstance(ov, dict) else {})
        check("22. terminal.open_visible has exact route + verify dependency",
              st == 200 and ov.get("request", {}).get("path") == "/term/create"
              and require.get("surface") == "vscode-terminal" and require.get("alive") is True)

        st, rv = _raw(base, "GET", "/operations/terminal.run_verified")
        text = json.dumps(rv)
        check("23. run_verified distinguishes completion source from output source",
              st == 200 and "read.buffer" in rv.get("completion_source", "")
              and "read.buffer" in rv.get("output_source", "")
              and rv.get("output_may_be_empty") is True and "/read" in text)
        nss = rv.get("not_success_sources", [])
        check("23b. run_verified no longer treats empty output as a failure",
              "empty command response" not in nss and "write.wrote" in nss
              and rv.get("success_when", {}).get("terminal", {}).get("surface")
              == "vscode-terminal")

        st, apis = _raw(base, "GET", "/apis")
        check("24. digest does not alter /apis (still 200, 13 rows)",
              st == 200 and isinstance(apis, list) and len(apis) == 13)

        st, _ = _raw(base, "GET", "/operations/nope")
        check("24b. unknown operation id -> 404", st == 404)
    finally:
        server.stop()


# --- 25-27: run-verified output/completion + leave-open + wrote!=ok -----------

def run_verified_tests():
    orig = discovery.discover_sessions
    try:
        discovery.discover_sessions = lambda **k: [_ds(47337, "RID-1")]
        prompt = "PS C:\\repo> "

        # A live vscode-terminal exists; run_result yields real output + completion.
        class RVClient(FakeClient):
            def __init__(self):
                super().__init__(
                    list_data=[{"id": "t1", "pid": 1, "bytes": 10, "alive": True,
                                "exitCode": None, "surface": "vscode-terminal"}],
                    read_data={"id": "t1", "bytes": 40, "buffer": prompt})

        rc = RVClient()

        class FakeRun:
            ok = True
            output = "dir-listing-here"
            warnings = []
            dispatch = {"bytes_written": 19, "meaning": "input accepted"}
            completion = {"end_marker_seen": True, "elapsed_ms": 12}
            status = {"exit_code": 0, "powershell_success": True,
                      "last_exit_code": None}
            session = {"codeapiRuntimeId": "RID-1", "workspace_uri": "file:///repo",
                       "port": 47337}

        rc.term.run_result = lambda *a, **k: FakeRun()
        # find/ensure need an alive vscode-terminal candidate matching cwd
        out = _capture(lambda: cli.cmd_term_run_verified(
            rc, _ns(json=True, rv_args=["Get-ChildItem"], cwd="C:\\repo",
                    shell="powershell", run_timeout=5, poll_ms=10, reap=False)))
        res = json.loads(out)
        check("25. run-verified returns /read output + completion evidence",
              res["output"]["clean"] == "dir-listing-here"
              and res["completionObserved"] is True and res["outputObserved"] is True)
        check("26. run-verified leaves the terminal open by default",
              res["terminal"]["leftOpen"] is True and res["reaped"] is False)

        # wrote alone must not yield ok:true (no completion, no output)
        class FakeRunNoOut(FakeRun):
            ok = False
            output = ""
            completion = {"end_marker_seen": False, "elapsed_ms": 5}
        rc.term.run_result = lambda *a, **k: FakeRunNoOut()
        out, code = _capture_exit(lambda: cli.cmd_term_run_verified(
            rc, _ns(json=True, rv_args=["x"], cwd="C:\\repo", shell="powershell",
                    run_timeout=5, poll_ms=10, reap=False)))
        res = json.loads(out) if out.strip().startswith("{") else {"ok": False}
        check("27. wrote alone does not produce ok:true",
              res.get("ok") is False and code == 1)
    finally:
        discovery.discover_sessions = orig


# --- 28-35: run-verified preflight parity, ok binding, leftOpen, pid ----------

def run_verified_gate_tests():
    """Both id-based and --cwd execution share the visible-terminal preflight;
    ok binds to completion + liveness (not output); leftOpen is observed; pid
    survives in both modes."""
    live_vsc = {"id": "t1", "pid": 7, "bytes": 10, "alive": True,
                "exitCode": None, "surface": "vscode-terminal"}

    class OkRun:
        ok = True
        output = "listing"
        warnings = []
        dispatch = {"bytes_written": 12, "meaning": "input accepted"}
        completion = {"end_marker_seen": True, "elapsed_ms": 9}
        status = {"exit_code": 0, "powershell_success": True, "last_exit_code": None}
        session = {"codeapiRuntimeId": "RID-1", "workspace_uri": "file:///repo",
                   "port": 47337}

    class ZeroOut(OkRun):
        output = ""

    # 28. zero-output command still succeeds (completion + live terminal), --cwd mode
    rc = FakeClient(list_data=[dict(live_vsc, pid=1)],
                    read_data={"id": "t1", "bytes": 40, "buffer": "PS C:\\repo> "})
    rc.term.run_result = lambda *a, **k: ZeroOut()
    out = _capture(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["Clear-Host"], cwd="C:\\repo",
                shell="powershell", run_timeout=5, poll_ms=10, reap=False)))
    res = json.loads(out)
    check("28. zero-output command still succeeds (completion, not output body)",
          res["ok"] is True and res["outputObserved"] is False
          and res["completionObserved"] is True)

    # 29. id-based mode rejects a headless surface:process terminal, no execution
    rc = FakeClient(list_data=[dict(live_vsc, surface="process")])
    ran = []
    rc.term.run_result = lambda *a, **k: (ran.append(1), OkRun())[1]
    out, code = _capture_exit(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["t1", "Get-ChildItem"], cwd=None,
                shell="powershell", run_timeout=5, poll_ms=10, reap=False)))
    res = json.loads(out)
    check("29. id-based mode rejects headless surface (no execution)",
          code == 1 and res["code"] == "SURFACE_MISMATCH"
          and res["executed"] is False and not ran)

    # 30. id-based mode rejects a dead terminal, no execution
    rc = FakeClient(list_data=[dict(live_vsc, alive=False, exitCode=0)])
    ran = []
    rc.term.run_result = lambda *a, **k: (ran.append(1), OkRun())[1]
    out, code = _capture_exit(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["t1", "x"], cwd=None, shell="powershell",
                run_timeout=5, poll_ms=10, reap=False)))
    res = json.loads(out)
    check("30. id-based mode rejects a dead terminal (no execution)",
          code == 1 and res["code"] == "TERMINAL_DEAD"
          and res["executed"] is False and not ran)

    # 31. id-based mode rejects an unlisted terminal
    rc = FakeClient(list_data=[])
    ran = []
    rc.term.run_result = lambda *a, **k: (ran.append(1), OkRun())[1]
    out, code = _capture_exit(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["tZ", "x"], cwd=None, shell="powershell",
                run_timeout=5, poll_ms=10, reap=False)))
    res = json.loads(out)
    check("31. id-based mode rejects an unlisted terminal (no execution)",
          code == 1 and res["code"] == "TERMINAL_NOT_LISTED" and not ran)

    # 32. terminal dying mid-run -> ok:false and leftOpen:false (not reaped)
    rc = FakeClient(list_data=[dict(live_vsc, pid=9)])

    def _die(*a, **k):
        rc._list = [dict(live_vsc, pid=9, alive=False, exitCode=1)]
        return OkRun()
    rc.term.run_result = _die
    out, code = _capture_exit(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["t1", "somecmd"], cwd=None, shell="powershell",
                run_timeout=5, poll_ms=10, reap=False)))
    res = json.loads(out)
    check("32. terminal dying mid-run -> ok:false, leftOpen:false, not reaped",
          code == 1 and res["ok"] is False
          and res["terminal"]["leftOpen"] is False and res["reaped"] is False
          and res["executed"] is True)

    # 33. id-based mode preserves the registry pid (defect 6)
    rc = FakeClient(list_data=[dict(live_vsc, pid=7)])
    rc.term.run_result = lambda *a, **k: OkRun()
    out = _capture(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["t1", "cmd"], cwd=None, shell="powershell",
                run_timeout=5, poll_ms=10, reap=False)))
    res = json.loads(out)
    check("33. id-based mode preserves pid from the registry",
          res["terminal"]["pid"] == 7 and res["ok"] is True)

    # 34. --cwd mode preserves the registry pid
    rc = FakeClient(list_data=[dict(live_vsc, pid=42)],
                    read_data={"id": "t1", "bytes": 40, "buffer": "PS C:\\repo> "})
    rc.term.run_result = lambda *a, **k: OkRun()
    out = _capture(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["cmd"], cwd="C:\\repo", shell="powershell",
                run_timeout=5, poll_ms=10, reap=False)))
    res = json.loads(out)
    check("34. --cwd mode preserves pid from the registry",
          res["terminal"]["pid"] == 42 and res["ok"] is True)

    # 35. --reap teardown -> reaped:true and leftOpen:false (observed, not assumed)
    rc = FakeClient(list_data=[dict(live_vsc, pid=9)])
    rc.term.run_result = lambda *a, **k: OkRun()
    out = _capture(lambda: cli.cmd_term_run_verified(
        rc, _ns(json=True, rv_args=["t1", "cmd"], cwd=None, shell="powershell",
                run_timeout=5, poll_ms=10, reap=True)))
    res = json.loads(out)
    check("35. --reap -> reaped:true, cleanupRequested:true, leftOpen:false",
          res["ok"] is True and res["reaped"] is True
          and res["cleanupRequested"] is True and res["leftOpen"] is False)


# --- 36-42: strict targeting for EVERY selector (defect 1) --------------------

def strict_targeting_tests():
    orig = discovery.discover_sessions
    try:
        c = CodeAPIClient()

        discovery.discover_sessions = lambda **k: [_ds(47337, "RID-1"),
                                                   _ds(47338, "RID-2")]
        try:
            c.target()
            check("36. no-selector + 2 runtimes -> TARGET_AMBIGUOUS", False)
        except CodeAPIError as e:
            check("36. no-selector + 2 runtimes -> TARGET_AMBIGUOUS",
                  "TARGET_AMBIGUOUS" in str(e))
        try:
            c.target(workspace="repo")
            check("37. ambiguous workspace substring -> TARGET_AMBIGUOUS", False)
        except CodeAPIError as e:
            check("37. ambiguous workspace substring -> TARGET_AMBIGUOUS",
                  "TARGET_AMBIGUOUS" in str(e))

        discovery.discover_sessions = lambda **k: [_ds(47337, "RID-1", sid="SID-aaa"),
                                                   _ds(47338, "RID-2", sid="SID-bbb")]
        try:
            c.target(session_id="SID")
            check("38. ambiguous session prefix -> TARGET_AMBIGUOUS", False)
        except CodeAPIError as e:
            check("38. ambiguous session prefix -> TARGET_AMBIGUOUS",
                  "TARGET_AMBIGUOUS" in str(e))
        rec = c.target(runtime_id="RID-2")
        check("39. runtime_id disambiguates to exactly one", rec.port == 47338)
        try:
            c.target(runtime_id="RID-X")
            check("40. unknown runtime_id -> TARGET_NOT_FOUND", False)
        except CodeAPIError as e:
            check("40. unknown runtime_id -> TARGET_NOT_FOUND",
                  "TARGET_NOT_FOUND" in str(e))

        discovery.discover_sessions = lambda **k: [_ds(47337, "RID-1")]
        rec = c.target()
        check("41. single runtime, no selector -> selected", rec.port == 47337)

        discovery.discover_sessions = lambda **k: []
        try:
            c.target()
            check("42. zero runtimes -> TARGET_NOT_FOUND", False)
        except CodeAPIError as e:
            check("42. zero runtimes -> TARGET_NOT_FOUND", "TARGET_NOT_FOUND" in str(e))
    finally:
        discovery.discover_sessions = orig


def main():
    native_route_tests()
    typed_field_tests()
    cli_format_tests()
    ensure_tests()
    open_visible_tests()
    digest_tests()
    run_verified_tests()
    run_verified_gate_tests()
    strict_targeting_tests()
    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 48}")
    print(f"terminal corridor: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
