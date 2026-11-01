"""TermClient — typed terminal control over the PUBLIC /term/* routes.

Every ordinary terminal operation targets the native public routes served by the
Python runtime (Phase 13D): GET /term/list, POST /term/create, POST /term/:id/write,
GET /term/:id/read, POST /term/:id/resize, DELETE /term/:id. The legacy
``codeApiTerm``-over-``/exec`` facade is retired for terminal I/O; /exec remains for
general extension-host JavaScript only (e.g. resolving the workspace name in
run_result).

Truth the routes carry and this client preserves:
  - ``surface`` — "vscode-terminal" (adapter-owned pty surfaced as codeapi:<id> in the
    window) or "process" (headless). Immutable per record. An unknown/absent surface
    can NEVER support a visible-terminal claim.
  - ``alive`` / ``exitCode`` — adapter node-pty state. A visible terminal is
    operationally open only when the SAME id reports surface:vscode-terminal AND
    alive:true. ``wrote`` from a write is bytes-accepted only — never success, never
    proof of life. Command output is the ``/term/:id/read`` buffer.
"""

import re
import sys
import time
import uuid as _uuid
from typing import List, Optional

from . import admission as _admission
from .errors import CodeAPIError
from .run_result import RunResult, _parse_status_line, _ps_wrapper
from .types import (SessionInfo, TermCreateResult, TermInfo, TermKillResult,
                    TermReadResult, TermResizeResult, TermWriteResult,
                    _analyze_buffer, _from_dict, _strip_ansi, _term_id)


def _entry_field(entry, name):
    """Read a field from either a TermInfo or a raw dict list entry."""
    if isinstance(entry, TermInfo):
        return getattr(entry, name, None)
    if isinstance(entry, dict):
        return entry.get(name)
    return None


class TermClient:
    """Typed Python interface to the public /term/* routes."""

    def __init__(self, client):
        self._c = client

    # -- terminal operations (native /term/* routes) ------------------------

    def list(self, **target_kw) -> List[TermInfo]:
        """GET /term/list -> [TermInfo] (carries surface/alive/exitCode)."""
        raw = self._c.get("/term/list", **target_kw)
        if isinstance(raw, list):
            return [_from_dict(TermInfo, item) for item in raw]
        return raw

    def create(self, id=None, cwd=None, shell=None, args=None,
               cols=None, rows=None, max_bytes=None, show=True,
               **target_kw) -> TermCreateResult:
        """POST /term/create -> TermCreateResult (carries surface). show:true asks the
        adapter to surface the pty as the VS Code integrated terminal codeapi:<id>."""
        body = {}
        if id is not None:
            body["id"] = _term_id(id)
        if cwd is not None:
            body["cwd"] = cwd
        if shell is not None:
            body["shell"] = shell
        if args is not None:
            body["args"] = args
        if cols is not None:
            body["cols"] = cols
        if rows is not None:
            body["rows"] = rows
        if max_bytes is not None:
            body["maxBytes"] = max_bytes
        if show:
            body["show"] = True
        raw = self._c.post("/term/create", body, **target_kw)
        return _from_dict(TermCreateResult, raw) if isinstance(raw, dict) else raw

    def write(self, term_id, data, enter=True, **target_kw) -> TermWriteResult:
        """POST /term/:id/write. JSON carries the payload directly — no /exec eval, so
        no base64 dance. Returns {id,wrote}: bytes ACCEPTED, not command success."""
        tid = _term_id(term_id)
        raw = self._c.post(f"/term/{tid}/write",
                           {"data": data, "enter": bool(enter)}, **target_kw)
        return _from_dict(TermWriteResult, raw) if isinstance(raw, dict) else raw

    def write_raw(self, term_id, text, enter=True, **target_kw) -> TermWriteResult:
        """Back-compat alias of write() (the native route needs no special escaping)."""
        return self.write(term_id, text, enter=enter, **target_kw)

    def read(self, term_id, tail=4000, clear=False,
             **target_kw) -> TermReadResult:
        """GET /term/:id/read -> TermReadResult. buffer is the raw ANSI/VT screen."""
        tid = _term_id(term_id)
        q = []
        if tail is not None:
            q.append(f"tail={int(tail)}")
        if clear:
            q.append("clear=true")
        path = f"/term/{tid}/read" + (("?" + "&".join(q)) if q else "")
        raw = self._c.get(path, **target_kw)
        return _from_dict(TermReadResult, raw) if isinstance(raw, dict) else raw

    def resize(self, term_id, cols, rows, **target_kw) -> TermResizeResult:
        """POST /term/:id/resize."""
        tid = _term_id(term_id)
        raw = self._c.post(f"/term/{tid}/resize",
                           {"cols": int(cols), "rows": int(rows)}, **target_kw)
        return _from_dict(TermResizeResult, raw) if isinstance(raw, dict) else raw

    def kill(self, term_id, **target_kw) -> TermKillResult:
        """DELETE /term/:id — cleanup/removal, NOT the liveness signal."""
        tid = _term_id(term_id)
        raw = self._c.delete(f"/term/{tid}", **target_kw)
        return _from_dict(TermKillResult, raw) if isinstance(raw, dict) else raw

    def run(self, term_id, command, timeout=120, poll_ms=200,
            settle_ms=800, settle=False, wait_ms=0,
            **target_kw) -> str:
        """Write a command, wait for completion, return cleaned output.

        Compatibility surface: still returns the command's text. It is now
        DERIVED from the execution envelope (run_evidence) rather than from a
        separate completion token, so there is one execution lifecycle. When
        admission was proven the text is what lies between the runtime
        markers; when it was not, no output can honestly be attributed to this
        command, so the raw tail is returned and a warning names which of the
        two failures occurred.

        Callers wanting the actual evidence — admitted vs completed, and the
        wrapped command's result — should use run_evidence().

        wait_ms and settle bound the OBSERVATION WINDOW. They no longer select
        a different detector.
        """
        tid = _term_id(term_id)
        deadline = time.time() + timeout

        if wait_ms > 0 or settle:
            return self._run_timed(tid, command, deadline, poll_ms, settle_ms,
                                   settle, wait_ms, **target_kw)

        # The execution envelope. See run_evidence.
        ev, output = self._run_envelope(tid, command, deadline, poll_ms,
                                        **target_kw)
        if not ev.begin_observed:
            print("warning: execution admission not observed within "
                  f"{timeout}s — the shell was never seen to start this "
                  "command. Transport succeeded; that is not the same thing. "
                  "Another application may own the terminal foreground.",
                  file=sys.stderr)
        elif not ev.end_observed:
            print(f"warning: command ADMITTED but not completed within "
                  f"{timeout}s; result unknown.", file=sys.stderr)
        return output

    # -- the execution lifecycle (Phase A) ----------------------------------
    #
    # ONE authoritative model. The previous one submitted the caller's command
    # and then a SECOND write, `echo CodeAPI_<uuid>_END`, and searched the
    # buffer for that literal token. Three defects, all of which the preserved
    # pager incident exercised at once:
    #
    #   the token existed VERBATIM in the submitted input, so PTY echo was
    #   the only thing standing between it and a false completion;
    #
    #   the second write was a SEPARATE submission, so whatever owned the
    #   terminal foreground received the completion token instead of the
    #   shell - the marker could be "completed" by an application that never
    #   ran the command;
    #
    #   and there was no BEGIN at all, so a missing token conflated "never
    #   admitted", "still running" and "finished but the token was lost".
    #
    # There is no exit code anywhere in that model either.

    def _terminal_row(self, tid, **target_kw):
        """The terminal's own /term/list row, or None."""
        for row in (self.list(**target_kw) or []):
            rid = row.id if isinstance(row, TermInfo) else row.get("id")
            if rid == tid:
                return row
        return None

    def _terminal_shell(self, tid, **target_kw):
        """The terminal's RECORDED spawn-time shell, from /term/list.

        The registry has held rec.shell since creation (extension.js), but the
        list serialization dropped it, so `run` against a pre-existing
        terminal had nothing to consult and assumed PowerShell. On this
        machine `term create` with no shell resolves COMSPEC, which is
        cmd.exe — so the DEFAULT terminal was the one being assumed wrong.

        None means the runtime did not report a shell. That is UNKNOWN, and
        unknown is refused rather than guessed.
        """
        for row in (self.list(**target_kw) or []):
            rid = row.id if isinstance(row, TermInfo) else row.get("id")
            if rid == tid:
                return (row.shell if isinstance(row, TermInfo)
                        else row.get("shell"))
        return None

    def _dialect(self, tid, **target_kw):
        """(shell, dialect, generation) from the terminal's own list row.

        One list call, so the shell and the generation describe the SAME
        observation — fetching them separately could straddle a kill/recreate
        and pair one incarnation's shell with another's generation.
        """
        row = self._terminal_row(tid, **target_kw)
        shell = (row.shell if isinstance(row, TermInfo)
                 else (row or {}).get("shell")) if row is not None else None
        generation = (row.generation if isinstance(row, TermInfo)
                      else (row or {}).get("generation")) if row is not None else None
        dialect = _admission.dialect_for_shell(shell)
        if not _admission.is_supported_dialect(dialect):
            raise _admission.UnsupportedShellError(tid, shell, dialect)
        return shell, dialect, generation

    def _submit(self, tid, command, shell, generation, **target_kw):
        """ONE atomic reserve-and-submit. Returns a dict of submission facts.

        Deliberately NOT self.write(): starting a shell command goes through
        the gated /submit, which checks generation, liveness, readiness and
        exclusive ownership and then writes in the SAME event-loop turn.
        Ordinary term write stays the raw interactive primitive so a caller
        can still answer a pager, and it is not a way to start a command.

        Python mints the execution id ONCE, here, and the same value is
        encoded into the envelope's BEGIN/END markers and carried into the
        evidence. Nothing downstream mints a second one.
        """
        execution_id = _admission.new_execution_id()
        envelope = _admission.build_envelope(command, execution_id, shell=shell)
        tidq = _term_id(tid)
        raw = self._c.post("/term/{}/submit".format(tidq),
                           {"executionId": execution_id, "data": envelope,
                            "expectedGeneration": generation}, **target_kw)
        r = raw if isinstance(raw, dict) else {}
        return {
            "execution_id": execution_id,
            "envelope": envelope,
            "submitted": bool(r.get("submitted")),
            "submission_accepted": bool(r.get("submissionAccepted")),
            "reason": r.get("reason"),
            "pre_submit_state": r.get("preSubmitState"),
            "observation_start_offset": r.get("observationStartOffset"),
            "generation": r.get("generation"),
            "active_execution_id": r.get("activeExecutionId"),
        }

    def _observe(self, tid, execution_id, deadline, poll_ms, stop_when=None,
                 **target_kw):
        """Poll, accumulating STICKY evidence (A20).

        Each read is classified and the results are OR-ed into flags that
        never go back down. Re-deriving state from only the current buffer
        would let the 1MB ring truncation turn an already-proven ADMITTED back
        into ADMISSION_UNKNOWN - evidence getting weaker as the command
        produces more output, which is backwards.
        """
        begin = end = False
        ps_ok = native = None
        buf = ""
        end_offset = retained = None
        while time.time() < deadline:
            time.sleep(poll_ms / 1000)
            r = self.read(tid, tail=200000, **target_kw)
            buf = r.buffer if isinstance(r, TermReadResult) else r.get("buffer", "")
            end_offset = getattr(r, "bytes", None)
            retained = len((buf or "").encode("utf-8"))
            seen = _admission.classify(buf, execution_id, submitted=True)
            begin = begin or seen.begin_observed
            if seen.end_observed and not end:
                end = True
                ps_ok = seen.powershell_success
                native = seen.native_exit_code
            if end or (stop_when is not None and stop_when(buf)):
                break
        return begin, end, ps_ok, native, buf, end_offset, retained

    def _run_envelope(self, tid, command, deadline, poll_ms, stop_when=None,
                      expected_generation=None, **target_kw):
        # Dialect FIRST, before anything is written. An unsupported shell
        # raises here, so a refused run submits zero bytes.
        _shell, dialect, generation = self._dialect(tid, **target_kw)
        if expected_generation is not None and expected_generation != generation:
            # The caller named a different incarnation. Refuse without even
            # asking the extension: nothing about this record answers them.
            return _admission.observed(
                execution_id=None, submitted=False, submission_accepted=False,
                begin_observed=False, end_observed=False, terminal_id=tid,
                terminal_generation=generation, wrapped_command=command,
                not_submitted_reason="TERMINAL_GENERATION_MISMATCH"), ""
        sub = self._submit(tid, command, dialect,
                           expected_generation or generation, **target_kw)
        execution_id = sub["execution_id"]
        if not sub["submitted"]:
            # PROVEN_NOT_SUBMITTED: zero bytes entered the PTY. Strictly
            # stronger than ADMISSION_UNKNOWN, and never conflated with it.
            return _admission.observed(
                execution_id=execution_id, submitted=False,
                submission_accepted=False, begin_observed=False,
                end_observed=False, terminal_id=tid,
                terminal_generation=sub.get("generation") or generation,
                wrapped_command=command,
                not_submitted_reason=sub.get("reason") or "UNKNOWN",
                pre_submit_state=sub.get("pre_submit_state")), ""
        start_offset = sub["observation_start_offset"]
        accepted = sub["submission_accepted"]
        begin, end, ps_ok, native, buf, end_offset, retained = self._observe(
            tid, execution_id, deadline, poll_ms, stop_when=stop_when,
            **target_kw)
        ev = _admission.observed(
            execution_id=execution_id, submitted=True,
            submission_accepted=accepted, begin_observed=begin,
            pre_submit_state=sub.get("pre_submit_state"),
            end_observed=end, powershell_success=ps_ok,
            native_exit_code=native, terminal_id=tid,
            terminal_generation=generation,
            wrapped_command=command, observation_start_offset=start_offset,
            observation_end_offset=end_offset, retained_buffer_bytes=retained)
        # Output is what lies BETWEEN the markers when admission was proven.
        # With no BEGIN there is no execution to attribute output to, so the
        # raw tail is returned rather than a claim about this command.
        output = (_admission.between_markers(buf, execution_id) if begin
                  else _strip_ansi(buf).rstrip())
        return ev, output

    def _run_timed(self, tid, command, deadline, poll_ms, settle_ms, settle,
                   wait_ms, **target_kw):
        """wait_ms / settle: same envelope, different stopping rule."""
        if wait_ms > 0:
            stop_at = time.time() + wait_ms / 1000
            stop = lambda _b: time.time() >= stop_at              # noqa: E731
        else:
            state = {"last": None, "since": time.time()}

            def stop(b):
                if b != state["last"]:
                    state["last"] = b
                    state["since"] = time.time()
                    return False
                return bool(b) and (time.time() - state["since"]) * 1000 >= settle_ms
        ev, output = self._run_envelope(tid, command, deadline, poll_ms,
                                        stop_when=stop, **target_kw)
        return output

    def run_evidence(self, term_id, command, timeout=120, poll_ms=200,
                     expected_generation=None, **target_kw):
        """The full execution evidence for one wrapped command.

        Returns an AdmissionEvidence plus the command's own output. This is
        the honest surface; run() returns only the text for compatibility and
        derives it from exactly this lifecycle.

        SHELL SCOPE: the dialect is selected from the terminal's RECORDED
        shell (/term/list), not from a parameter and not from the environment.
        A terminal whose shell has no tested envelope raises
        UnsupportedShellError BEFORE any write, so a refused run submits zero
        bytes. PowerShell/pwsh is the only supported dialect; cmd.exe - which
        is the Windows DEFAULT here, via COMSPEC - is refused explicitly.
        """
        tid = _term_id(term_id)
        return self._run_envelope(tid, command, time.time() + timeout,
                                  poll_ms,
                                  expected_generation=expected_generation,
                                  **target_kw)

    # -- higher-level operations --------------------------------------------

    def _wire_state(self, term_id, **target_kw) -> dict:
        """The adapter-sourced surface/alive/exitCode for one id, from /term/list.
        Missing id -> all None (surface unknown => not a visible terminal)."""
        state = {"surface": None, "alive": None, "exitCode": None}
        try:
            for t in self.list(**target_kw):
                if _entry_field(t, "id") == term_id:
                    state["surface"] = _entry_field(t, "surface")
                    state["alive"] = _entry_field(t, "alive")
                    state["exitCode"] = _entry_field(t, "exitCode")
                    break
        except CodeAPIError:
            pass
        return state

    def inspect(self, term_id, tail=4000, **target_kw) -> dict:
        """Read a terminal's tail + analyze shell/cwd/idle, and attach the
        adapter's surface/alive/exitCode so callers never lose the visibility and
        liveness truth."""
        tid = _term_id(term_id)
        result = self.read(tid, tail=tail, **target_kw)
        buf = result.buffer if isinstance(result, TermReadResult) else result.get("buffer", "")
        byte_count = result.bytes if isinstance(result, TermReadResult) else result.get("bytes", 0)
        info = _analyze_buffer(_strip_ansi(buf), tid)
        info["bytes"] = byte_count
        info.update(self._wire_state(tid, **target_kw))
        return info

    def find(self, cwd=None, shell=None, idle_only=True,
             **target_kw) -> List[dict]:
        """List terminals, read each, filter by cwd/shell/idle criteria. Each result
        carries surface/alive/exitCode (via inspect)."""
        terms = self.list(**target_kw)
        if not isinstance(terms, list):
            return []
        results = []
        for t in terms:
            tid = _entry_field(t, "id") or ""
            info = self.inspect(tid, **target_kw)
            if cwd and info.get("cwd"):
                detected = info["cwd"].lower().rstrip("\\")
                want = cwd.lower().rstrip("\\")
                if want != detected and not detected.endswith("\\" + want):
                    continue
            elif cwd and not info.get("cwd"):
                continue
            if shell and info.get("shell"):
                if shell.lower() != info["shell"].lower():
                    continue
            elif shell and not info.get("shell"):
                continue
            if idle_only and not info.get("idle", False):
                continue
            results.append(info)
        return results

    def ensure(self, cwd, shell=None, show=True,
               **target_kw) -> dict:
        """Reuse a matching terminal or create one, honoring surface + liveness:

          - never reuse a DEAD terminal as live;
          - a visible request (show=True) reuses ONLY an alive surface:vscode-terminal;
          - a headless request (show=False) is never labeled visible;
          - a newly created visible terminal is verified through /term/list (the exact
            id must report surface:vscode-terminal and alive:true).
        """
        candidates = self.find(cwd=cwd, shell=shell, **target_kw)
        for c in candidates:
            if c.get("alive") is not True:
                continue                       # do not resurrect a dead terminal
            if show and c.get("surface") != "vscode-terminal":
                continue                       # visible must not reuse a process surface
            return {"action": "reused", "verified": True, "terminal": c}

        create_shell = shell
        if shell and "." not in shell and "\\" not in shell:
            _map = {"powershell": "powershell.exe", "cmd": "cmd.exe"}
            create_shell = _map.get(shell.lower(), shell)
        result = self.create(cwd=cwd, shell=create_shell, show=show, **target_kw)
        time.sleep(1.5)

        tid = result.id if isinstance(result, TermCreateResult) else _entry_field(result, "id")
        surface = result.surface if isinstance(result, TermCreateResult) else None
        state = self._wire_state(tid, **target_kw) if tid else {"surface": surface, "alive": None, "exitCode": None}
        if state.get("surface") is not None:
            surface = state["surface"]
        alive = state.get("alive")
        exit_code = state.get("exitCode")
        if show:
            verified = surface == "vscode-terminal" and alive is True
        else:
            verified = alive is True
        info = {
            "id": tid,
            "shell": result.shell if isinstance(result, TermCreateResult) else None,
            "cwd": result.cwd if isinstance(result, TermCreateResult) else None,
            "pid": result.pid if isinstance(result, TermCreateResult) else None,
            "surface": surface, "alive": alive, "exitCode": exit_code,
        }
        return {"action": "created", "verified": verified, "terminal": info}

    def preflight_visible(self, term_id, **target_kw) -> dict:
        """Shared precondition for verified execution against a VISIBLE terminal.

        Reads /term/list ONCE and requires the exact id to be present with
        surface:vscode-terminal, alive:true, exitCode:null. Both the id-based and the
        --cwd execution paths gate on this identical contract — an id selects a
        resource, it does not bypass the resource's state. Returns a structured verdict
        whose ``terminal`` block is the FULL registry record (id/pid/bytes/surface/
        alive/exitCode), so downstream evidence (e.g. pid) never has to be reconstructed
        from a create response that may be empty in id-based mode.

        verdict = {"ok": bool, "code": str, "listed": bool,
                   "terminal": {id,pid,bytes,surface,alive,exitCode}}
        codes: VERIFIED | STATE_UNAVAILABLE | TERMINAL_NOT_LISTED | SURFACE_MISMATCH |
               TERMINAL_DEAD | STATE_INCOMPLETE
        """
        tid = _term_id(term_id)
        record = None
        try:
            for t in self.list(**target_kw):
                if _entry_field(t, "id") == tid:
                    record = t
                    break
        except CodeAPIError as e:
            return {"ok": False, "code": "STATE_UNAVAILABLE", "listed": False,
                    "error": str(e), "terminal": {"id": tid}}
        if record is None:
            return {"ok": False, "code": "TERMINAL_NOT_LISTED", "listed": False,
                    "terminal": {"id": tid}}
        surface = _entry_field(record, "surface")
        alive = _entry_field(record, "alive")
        exit_code = _entry_field(record, "exitCode")
        term = {"id": tid, "pid": _entry_field(record, "pid"),
                "bytes": _entry_field(record, "bytes"),
                "surface": surface, "alive": alive, "exitCode": exit_code}
        if surface != "vscode-terminal":
            return {"ok": False, "code": "SURFACE_MISMATCH", "listed": True,
                    "terminal": term}
        if alive is not True:
            return {"ok": False, "code": "TERMINAL_DEAD", "listed": True,
                    "terminal": term}
        if exit_code is not None:
            return {"ok": False, "code": "STATE_INCOMPLETE", "listed": True,
                    "terminal": term}
        return {"ok": True, "code": "VERIFIED", "listed": True, "terminal": term}

    # -- first-class visible-terminal composite -----------------------------

    def open_visible(self, runtime_id=None, shell="powershell.exe", cwd=None,
                     term_id=None, port=None) -> dict:
        """Resolve ONE adapter runtime, create a visible terminal, and verify it.

        Strict: selects an adapter runtime with live terminal routes by codeapiRuntimeId
        (or an explicit port), FAILING on ambiguity rather than choosing the first. Then
        POST /term/create show:true, requires surface:vscode-terminal in the response,
        GET /term/list for the same id, and requires surface:vscode-terminal + alive:true
        + exitCode:null. Returns a single structured result; failures carry a code.
        """
        from . import discovery

        def _cand(s):
            return {"codeapiRuntimeId": s.codeapi_runtime_id,
                    "codeapiWorkspaceId": s.codeapi_workspace_id,
                    "workspaceUri": s.workspace_uri,
                    "serverPort": s.server_port or s.port,
                    "port": s.port}

        def _fail(status, error, target=None, terminal=None, candidates=None):
            out = {"operation": "terminal.open_visible", "status": status, "error": error}
            if target is not None:
                out["target"] = target
            if terminal is not None:
                out["terminal"] = terminal
            if candidates is not None:
                out["candidates"] = candidates
            return out

        sessions = discovery.discover_sessions()
        adapters = [s for s in sessions
                    if s.healthy and s.runtime_type == "adapter" and s.terminals_usable]
        if runtime_id:
            matches = [s for s in adapters if s.codeapi_runtime_id == runtime_id]
        elif port:
            matches = [s for s in adapters if port in (s.port, s.server_port)]
        else:
            matches = adapters
        if not matches:
            return _fail("TERMINAL_ROUTES_UNAVAILABLE",
                         "no adapter runtime with live terminal routes matched the target",
                         candidates=[_cand(s) for s in adapters])
        if len(matches) > 1:
            return _fail("TARGET_AMBIGUOUS",
                         f"{len(matches)} adapter runtimes match; pin one with "
                         "--runtime-id (codeapiRuntimeId) or --port",
                         candidates=[_cand(s) for s in matches])
        tgt = matches[0]
        target_block = {"codeapiRuntimeId": tgt.codeapi_runtime_id,
                        "codeapiWorkspaceId": tgt.codeapi_workspace_id,
                        "workspaceUri": tgt.workspace_uri,
                        "serverPort": tgt.server_port or tgt.port}
        pin = {"port": tgt.port}

        try:
            created = self.create(id=term_id, cwd=cwd, shell=shell, show=True, **pin)
        except CodeAPIError as e:
            return _fail("CREATE_FAILED", str(e), target_block)
        if not isinstance(created, TermCreateResult) or not created.id:
            return _fail("CREATE_FAILED", f"unexpected create response: {created!r}",
                         target_block)
        if created.surface != "vscode-terminal":
            return _fail("SURFACE_MISMATCH",
                         f"create returned surface={created.surface!r}, expected "
                         "vscode-terminal",
                         target_block,
                         terminal={"id": created.id, "pid": created.pid,
                                   "surface": created.surface})

        try:
            listing = self.list(**pin)
        except CodeAPIError as e:
            return _fail("STATE_INCOMPLETE", f"/term/list failed: {e}", target_block,
                         terminal={"id": created.id})
        entry = None
        for t in (listing if isinstance(listing, list) else []):
            if _entry_field(t, "id") == created.id:
                entry = t
                break
        if entry is None:
            return _fail("TERMINAL_NOT_LISTED",
                         f"created terminal {created.id} not present in /term/list",
                         target_block, terminal={"id": created.id})
        e_surface = _entry_field(entry, "surface")
        e_alive = _entry_field(entry, "alive")
        e_exit = _entry_field(entry, "exitCode")
        e_pid = _entry_field(entry, "pid")
        if e_surface != "vscode-terminal":
            return _fail("SURFACE_MISMATCH",
                         f"/term/list reports surface={e_surface!r}", target_block,
                         terminal={"id": created.id, "surface": e_surface})
        if e_alive is not True:
            return _fail("TERMINAL_DEAD",
                         f"/term/list reports alive={e_alive!r} exitCode={e_exit!r}",
                         target_block,
                         terminal={"id": created.id, "surface": e_surface,
                                   "alive": e_alive, "exitCode": e_exit})
        if e_exit is not None:
            return _fail("STATE_INCOMPLETE",
                         f"live terminal reports non-null exitCode={e_exit!r}",
                         target_block, terminal={"id": created.id})

        return {
            "operation": "terminal.open_visible",
            "status": "verified",
            "target": target_block,
            "terminal": {"id": created.id,
                         "pid": e_pid if e_pid is not None else created.pid,
                         "shell": created.shell, "cwd": created.cwd,
                         "surface": e_surface, "alive": e_alive, "exitCode": e_exit},
            "channels": {"write": f"/term/{created.id}/write",
                         "read": f"/term/{created.id}/read",
                         "state": "/term/list",
                         "delete": f"/term/{created.id}"},
            "claim": "CODEAPI created and verified an alive VS Code integrated terminal.",
        }

    # -- verified execution --------------------------------------------------

    def run_result(self, term_id, command, shell=None,
                   timeout=120, poll_ms=200, **target_kw):
        """Execute a command with full evidence capture. Returns RunResult.

        COMPATIBILITY FORMATTER over the one execution engine (B1). It used to
        carry a SECOND, independent marker protocol, and every difference
        between the two was a defect in this one:

          it wrote the start marker as a SEPARATE write — and the cmd branch
          wrote FOUR times — so a foreground application could consume any of
          them independently of the command they were evidence for, which is
          the preserved pager incident exactly;

          its markers appeared VERBATIM in the submitted text, so PTY echo
          alone could satisfy the detector;

          it never cleared $LASTEXITCODE before the command, so a stale native
          exit code from an earlier execution could be reported as this one's
          result;

          and it preferred $LASTEXITCODE over $?, so a compound command's
          intermediate native step outranked the statement that actually
          ended the command;

          and `shell` was a PARAMETER defaulting to "powershell", so calling
          it against a default cmd.exe terminal submitted PowerShell syntax
          by construction.

        All of that is gone: it now delegates to _run_envelope, which selects
        the dialect from the terminal's recorded shell, submits ONE
        non-echoable envelope, and returns admission evidence. The RunResult
        shape is preserved and derived from that evidence.

        `shell` is accepted and IGNORED, kept only so existing callers do not
        break; the terminal's own recorded shell is authoritative.
        """
        tid = _term_id(term_id)
        warnings = []
        if shell is not None:
            warnings.append(
                "the `shell` argument is ignored; the dialect comes from the "
                "terminal's recorded shell")

        s = self._c._resolve(**target_kw)
        info = s.info
        sid = info.id if isinstance(info, SessionInfo) else info.get("id", "")
        pid_val = info.pid if isinstance(info, SessionInfo) else info.get("pid", 0)
        ws_uri = (info.workspaceUri if isinstance(info, SessionInfo)
                  else info.get("workspaceUri", ""))
        runtime_id = (info.codeapiRuntimeId if isinstance(info, SessionInfo)
                      else info.get("codeapiRuntimeId"))
        try:
            ws_name = self._c._http(
                "POST", f"{s.base_url}/exec",
                body="return vscode.workspace.name",
                content_type="text/plain")
        except CodeAPIError:
            ws_name = None
            warnings.append("could not resolve workspace name")
        session_ev = {"id": sid, "port": s.port, "pid": pid_val,
                      "codeapiRuntimeId": runtime_id,
                      "workspace_name": ws_name, "workspace_uri": ws_uri}

        pinned = {"port": s.port}

        try:
            pre = self.inspect(tid, **pinned)
        except CodeAPIError as e:
            pre = {"id": tid, "error": str(e)}
            warnings.append(f"pre-state capture failed: {e}")

        t0 = time.time()
        ev, output = self._run_envelope(
            tid, command, t0 + timeout, poll_ms, **pinned)
        elapsed_ms = int((time.time() - t0) * 1000)

        dispatch_ev = {
            "write_ok": bool(ev.submission_accepted),
            "bytes_written": None,          # the envelope's byte count is not
                                            # evidence of anything; see below
            "meaning": "input accepted — NOT command execution",
            "start_marker": _admission.begin_marker(ev.execution_id),
            "end_marker": _admission.end_marker_prefix(ev.execution_id),
            "execution_id": ev.execution_id,
        }
        completion_ev = {
            "start_marker_seen": ev.begin_observed,
            "end_marker_seen": ev.end_observed,
            "timed_out": not ev.end_observed,
            "elapsed_ms": elapsed_ms,
            "status": ev.status,
            "observation_start_offset": ev.observation_start_offset,
            "observation_end_offset": ev.observation_end_offset,
            "possible_output_loss": ev.possible_output_loss,
        }
        if not ev.begin_observed:
            warnings.append(
                "execution admission was not observed: the shell was never "
                "seen to start this command")
        elif not ev.end_observed:
            warnings.append(
                f"command ADMITTED but not completed within {timeout}s")

        # exit_code stays -1 for "no result", the value this surface has
        # always used for that. It is NOT conflated with a real -1 exit,
        # because a real one only appears when end_marker_seen is true.
        # The three keys run_result.py has always produced, unchanged. An
        # interim version also carried a `result_kind`, which was never part of
        # this compat surface and has been dropped along with the taxonomy
        # behind it; consumers needing more than the integer read the evidence.
        status_ev = {
            "exit_code": ev.exit_code if ev.exit_code is not None else -1,
            "powershell_success": bool(ev.powershell_success),
            "last_exit_code": ev.native_exit_code,
        }
        if ev.anomalies:
            warnings.extend(ev.anomalies)

        try:
            post = self.inspect(tid, **pinned)
        except CodeAPIError as e:
            post = {"id": tid, "error": str(e)}
            warnings.append(f"post-state capture failed: {e}")

        # ok requires COMPLETED — admission alone is not success, and neither
        # is a zero exit code without the envelope having been observed.
        ok = (ev.begin_observed and ev.end_observed
              and status_ev["exit_code"] == 0)

        return RunResult(
            ok=ok, run_id=f"CodeAPI_{ev.execution_id}",
            command=command, output=output, warnings=warnings,
            session=session_ev, terminal_before=pre,
            dispatch=dispatch_ev, completion=completion_ev,
            status=status_ev, terminal_after=post)

    def run_verified(self, term_id, command, shell=None,
                     timeout=120, poll_ms=200, **target_kw):
        """Execute and raise CodeAPIError if not ok.

        Unchanged externally; it now sits on the one execution engine via
        run_result. An unsupported terminal dialect raises
        UnsupportedShellError — itself a CodeAPIError — before any write, so
        this surface also fails closed rather than submitting the wrong
        syntax.
        """
        r = self.run_result(term_id, command, shell=shell,
                            timeout=timeout, poll_ms=poll_ms, **target_kw)
        if not r.ok:
            reasons = list(r.warnings)
            if r.status and r.status.get("exit_code", -1) != 0:
                reasons.append(f"exit_code={r.status.get('exit_code')}")
            raise CodeAPIError(
                f"run_verified failed: {'; '.join(reasons)}")
        return r
