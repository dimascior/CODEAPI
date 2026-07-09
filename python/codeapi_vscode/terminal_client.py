"""TermClient — typed terminal control via /exec.

Ported from the retired seed CLI (Phase 10). Hides JavaScript string
construction behind Python methods; callers never write JS. Behavior is the
seed's, byte-for-byte. Routes through /exec until the extension gains native
/term/* routes (Phase 13, delegation to the JS adapter per PYTHON_PORT_DECISION).
"""

import base64
import json
import re
import sys
import time
import uuid as _uuid
from typing import List

from .errors import CodeAPIError
from .run_result import RunResult, _parse_status_line, _ps_wrapper
from .types import (SessionInfo, TermCreateResult, TermInfo, TermKillResult,
                    TermReadResult, TermResizeResult, TermWriteResult,
                    _analyze_buffer, _from_dict, _strip_ansi, _term_id)


class TermClient:
    """Typed Python interface to codeApiTerm, routed through /exec."""

    def __init__(self, client):
        self._c = client

    def _exec(self, js, **target_kw):
        return self._c.exec_js(js, **target_kw)

    # -- terminal operations ---------------------------------------------

    def list(self, **target_kw) -> List[TermInfo]:
        raw = self._exec("return codeApiTerm.list()", **target_kw)
        if isinstance(raw, list):
            return [_from_dict(TermInfo, item) for item in raw]
        return raw

    def create(self, id=None, cwd=None, shell=None, args=None,
               cols=None, rows=None, max_bytes=None, show=True,
               **target_kw) -> TermCreateResult:
        opts = {}
        if id is not None:
            opts["id"] = _term_id(id)
        if cwd is not None:
            opts["cwd"] = cwd
        if shell is not None:
            opts["shell"] = shell
        if args is not None:
            opts["args"] = args
        if cols is not None:
            opts["cols"] = cols
        if rows is not None:
            opts["rows"] = rows
        if max_bytes is not None:
            opts["maxBytes"] = max_bytes
        if show:
            opts["show"] = True
        js = f"return codeApiTerm.create({json.dumps(opts)})"
        raw = self._exec(js, **target_kw)
        return _from_dict(TermCreateResult, raw) if isinstance(raw, dict) else raw

    def write(self, term_id, data, enter=True, **target_kw) -> TermWriteResult:
        """Write data to a terminal. Uses Base64 transfer so the payload
        crosses /exec's JavaScript eval without any quoting issues."""
        tid = _term_id(term_id)
        b64 = base64.b64encode(data.encode("utf-8")).decode("ascii")
        enter_js = "true" if enter else "false"
        js = (f"return codeApiTerm.write('{tid}', "
              f"Buffer.from('{b64}','base64').toString(), "
              f"{{enter:{enter_js}}})")
        raw = self._exec(js, **target_kw)
        return _from_dict(TermWriteResult, raw) if isinstance(raw, dict) else raw

    def write_raw(self, term_id, text, enter=True, **target_kw) -> TermWriteResult:
        """Write without Base64 — only safe for text with no JS-special chars.
        Prefer write() for arbitrary content."""
        tid = _term_id(term_id)
        safe_text = json.dumps(text)  # JSON-encode handles escaping for JS string
        enter_js = "true" if enter else "false"
        js = f"return codeApiTerm.write('{tid}', {safe_text}, {{enter:{enter_js}}})"
        raw = self._exec(js, **target_kw)
        return _from_dict(TermWriteResult, raw) if isinstance(raw, dict) else raw

    def read(self, term_id, tail=4000, clear=False,
             **target_kw) -> TermReadResult:
        tid = _term_id(term_id)
        opts = {"tail": tail}
        if clear:
            opts["clear"] = True
        js = f"return codeApiTerm.read('{tid}', {json.dumps(opts)})"
        raw = self._exec(js, **target_kw)
        return _from_dict(TermReadResult, raw) if isinstance(raw, dict) else raw

    def resize(self, term_id, cols, rows, **target_kw) -> TermResizeResult:
        tid = _term_id(term_id)
        js = f"return codeApiTerm.resize('{tid}', {int(cols)}, {int(rows)})"
        raw = self._exec(js, **target_kw)
        return _from_dict(TermResizeResult, raw) if isinstance(raw, dict) else raw

    def kill(self, term_id, **target_kw) -> TermKillResult:
        tid = _term_id(term_id)
        js = f"return codeApiTerm.kill('{tid}')"
        raw = self._exec(js, **target_kw)
        return _from_dict(TermKillResult, raw) if isinstance(raw, dict) else raw

    def run(self, term_id, command, timeout=120, poll_ms=200,
            settle_ms=800, settle=False, wait_ms=0,
            **target_kw) -> str:
        """Write a command, wait for completion, return cleaned output.

        Completion detection modes (in priority order):
          wait_ms > 0  — fixed delay snapshot (fastest, least reliable)
          settle=True  — wait for output to stop changing
          default      — sentinel marker: echo a unique token after the
                         command and poll until it appears
        """
        tid = _term_id(term_id)

        # Flush prior output
        self.read(tid, clear=True, **target_kw)

        # Write the actual command
        self.write(tid, command, **target_kw)

        deadline = time.time() + timeout

        # Mode 1: fixed-delay snapshot
        if wait_ms > 0:
            time.sleep(wait_ms / 1000)
            result = self.read(tid, tail=200000, **target_kw)
            buf = result.buffer if isinstance(result, TermReadResult) else result.get("buffer", "")
            return _strip_ansi(buf).rstrip()

        # Mode 2: settle detection
        if settle:
            last = ""
            last_change = time.time()
            while time.time() < deadline:
                time.sleep(poll_ms / 1000)
                result = self.read(tid, tail=200000, **target_kw)
                buf = result.buffer if isinstance(result, TermReadResult) else result.get("buffer", "")
                if buf != last:
                    last = buf
                    last_change = time.time()
                elif buf and (time.time() - last_change) * 1000 >= settle_ms:
                    return _strip_ansi(last).rstrip()
            print(f"warning: output never settled within {timeout}s; returning partial.",
                  file=sys.stderr)
            return _strip_ansi(last).rstrip()

        # Mode 3: sentinel marker
        marker = f"CodeAPI_{_uuid.uuid4().hex}_END"
        self.write(tid, f"echo {marker}", **target_kw)
        marker_re = re.compile(r"(?m)^\s*" + re.escape(marker))

        while time.time() < deadline:
            time.sleep(poll_ms / 1000)
            result = self.read(tid, tail=200000, **target_kw)
            buf = result.buffer if isinstance(result, TermReadResult) else result.get("buffer", "")
            clean = _strip_ansi(buf)
            m = marker_re.search(clean)
            if m:
                lines = clean[:m.start()].split("\n")
                lines = [ln for ln in lines if marker not in ln]
                return "\n".join(lines).rstrip()

        print(f"warning: completion token not seen within {timeout}s. "
              "Command may be interactive (try --settle).",
              file=sys.stderr)
        result = self.read(tid, tail=200000, **target_kw)
        buf = result.buffer if isinstance(result, TermReadResult) else result.get("buffer", "")
        return _strip_ansi(buf).rstrip()

    # -- higher-level operations --------------------------------------------

    def inspect(self, term_id, tail=4000, **target_kw) -> dict:
        """Read a terminal's tail and analyze shell, cwd, idle state."""
        tid = _term_id(term_id)
        result = self.read(tid, tail=tail, **target_kw)
        buf = result.buffer if isinstance(result, TermReadResult) else result.get("buffer", "")
        byte_count = result.bytes if isinstance(result, TermReadResult) else result.get("bytes", 0)
        info = _analyze_buffer(_strip_ansi(buf), tid)
        info["bytes"] = byte_count
        return info

    def find(self, cwd=None, shell=None, idle_only=True,
             **target_kw) -> List[dict]:
        """List terminals, read each, filter by cwd/shell/idle criteria."""
        terms = self.list(**target_kw)
        if not isinstance(terms, list):
            return []
        results = []
        for t in terms:
            tid = t.id if isinstance(t, TermInfo) else t.get("id", "")
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
        """Reuse a matching idle terminal or create a new one.
        Short shell names like 'powershell' are used for find() matching
        and mapped to binary paths like 'powershell.exe' for create()."""
        candidates = self.find(cwd=cwd, shell=shell, **target_kw)
        if candidates:
            return {"action": "reused", "terminal": candidates[0]}
        create_shell = shell
        if shell and "." not in shell and "\\" not in shell:
            _map = {"powershell": "powershell.exe", "cmd": "cmd.exe"}
            create_shell = _map.get(shell.lower(), shell)
        result = self.create(cwd=cwd, shell=create_shell, show=show,
                             **target_kw)
        time.sleep(1.5)
        if isinstance(result, TermCreateResult):
            info = {"id": result.id, "shell": result.shell,
                    "cwd": result.cwd, "pid": result.pid}
        else:
            info = result
        return {"action": "created", "terminal": info}

    # -- verified execution --------------------------------------------------

    def run_result(self, term_id, command, shell="powershell",
                   timeout=120, poll_ms=200, **target_kw):
        """Execute a command with full evidence capture. Returns RunResult."""
        tid = _term_id(term_id)
        run_id = _uuid.uuid4().hex[:12]
        warnings = []

        s = self._c._resolve(**target_kw)
        info = s.info
        sid = info.id if isinstance(info, SessionInfo) else info.get("id", "")
        pid_val = info.pid if isinstance(info, SessionInfo) else info.get("pid", 0)
        ws_uri = (info.workspaceUri if isinstance(info, SessionInfo)
                  else info.get("workspaceUri", ""))
        try:
            ws_name = self._c._http(
                "POST", f"{s.base_url}/exec",
                body="return vscode.workspace.name",
                content_type="text/plain")
        except CodeAPIError:
            ws_name = None
            warnings.append("could not resolve workspace name")
        session_ev = {"id": sid, "port": s.port, "pid": pid_val,
                      "workspace_name": ws_name, "workspace_uri": ws_uri}

        pinned = {"port": s.port}

        try:
            pre = self.inspect(tid, **pinned)
        except CodeAPIError as e:
            pre = {"id": tid, "error": str(e)}
            warnings.append(f"pre-state capture failed: {e}")

        self.read(tid, clear=True, **pinned)

        start_marker = f"__CodeAPI_{run_id}_START__"
        end_marker = f"__CodeAPI_{run_id}_END__"
        status_marker = f"__CodeAPI_{run_id}_STATUS__"

        if shell == "powershell":
            self.write(tid, f"Write-Output '{start_marker}'", **pinned)
            wrapped = _ps_wrapper(run_id, command)
            wr = self.write(tid, wrapped, **pinned)
        else:
            self.write(tid, f"echo {start_marker}", **pinned)
            self.write(tid, command, **pinned)
            wr = self.write(
                tid, f"echo {status_marker} exit_code=%ERRORLEVEL%", **pinned)
            self.write(tid, f"echo {end_marker}", **pinned)

        write_ok = isinstance(wr, TermWriteResult)
        dispatch_ev = {
            "base64_transfer": True, "write_ok": write_ok,
            "bytes_written": wr.wrote if write_ok else 0,
            "start_marker": start_marker, "end_marker": end_marker,
        }

        t0 = time.time()
        deadline = t0 + timeout
        start_re = re.compile(
            r"(?m)^\s*" + re.escape(start_marker) + r"\s*$")
        end_re = re.compile(
            r"(?m)^\s*" + re.escape(end_marker) + r"\s*$")
        start_seen = False
        end_seen = False
        status_parsed = None
        final_buf = ""

        while time.time() < deadline:
            time.sleep(poll_ms / 1000)
            rd = self.read(tid, tail=200000, **pinned)
            buf = (rd.buffer if isinstance(rd, TermReadResult)
                   else rd.get("buffer", ""))
            clean = _strip_ansi(buf)
            if start_re.search(clean):
                start_seen = True
            if end_re.search(clean):
                end_seen = True
                final_buf = clean
                for line in clean.split("\n"):
                    parsed = _parse_status_line(line, run_id)
                    if parsed is not None:
                        status_parsed = parsed
                break

        elapsed_ms = int((time.time() - t0) * 1000)
        timed_out = not end_seen
        if timed_out:
            warnings.append(f"end marker not seen within {timeout}s")
            rd = self.read(tid, tail=200000, **pinned)
            buf = (rd.buffer if isinstance(rd, TermReadResult)
                   else rd.get("buffer", ""))
            final_buf = _strip_ansi(buf)
            if start_re.search(final_buf):
                start_seen = True

        completion_ev = {
            "start_marker_seen": start_seen,
            "end_marker_seen": end_seen,
            "timed_out": timed_out,
            "elapsed_ms": elapsed_ms,
        }

        output = ""
        sm = start_re.search(final_buf)
        if sm:
            end_pos = len(final_buf)
            for pat_str in (re.escape(status_marker),
                            re.escape(end_marker)):
                em = re.search(pat_str, final_buf[sm.end():])
                if em:
                    end_pos = sm.end() + em.start()
                    break
            body = final_buf[sm.end():end_pos]
            out_lines = [ln.rstrip("\r") for ln in body.split("\n")
                         if run_id not in ln]
            while out_lines and not out_lines[0].strip():
                out_lines.pop(0)
            while out_lines and not out_lines[-1].strip():
                out_lines.pop()
            output = "\n".join(out_lines)

        if status_parsed is None:
            status_ev = {"exit_code": -1, "powershell_success": False,
                         "last_exit_code": None}
            if not timed_out:
                warnings.append("status marker not parsed")
        else:
            status_ev = status_parsed

        try:
            post = self.inspect(tid, **pinned)
        except CodeAPIError as e:
            post = {"id": tid, "error": str(e)}
            warnings.append(f"post-state capture failed: {e}")

        ok = (start_seen and end_seen and not timed_out
              and status_ev.get("exit_code", -1) == 0)

        return RunResult(
            ok=ok, run_id=f"CodeAPI_{run_id}",
            command=command, output=output, warnings=warnings,
            session=session_ev, terminal_before=pre,
            dispatch=dispatch_ev, completion=completion_ev,
            status=status_ev, terminal_after=post)

    def run_verified(self, term_id, command, shell="powershell",
                     timeout=120, poll_ms=200, **target_kw):
        """Execute and raise CodeAPIError if not ok."""
        r = self.run_result(term_id, command, shell=shell,
                            timeout=timeout, poll_ms=poll_ms, **target_kw)
        if not r.ok:
            reasons = list(r.warnings)
            if r.status and r.status.get("exit_code", -1) != 0:
                reasons.append(f"exit_code={r.status.get('exit_code')}")
            raise CodeAPIError(
                f"run_verified failed: {'; '.join(reasons)}")
        return r
