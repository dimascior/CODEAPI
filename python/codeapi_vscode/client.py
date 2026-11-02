"""CodeAPIClient — base HTTP client for the CODEAPI extension server.

Ported from the retired seed CLI (Phase 10). Stdlib urllib only; ports
3637-3646; targeting by port / id-prefix / workspace-substring. Behavior is the
seed's, byte-for-byte.

Replaces the Node client (cli.ts) and the old requests-based
legacy-seed/mcp-server/app/vscode_api_client.py.
"""

import json
import os
import urllib.error
import urllib.request
from typing import List, Optional

from .errors import CodeAPIError
from .terminal_client import TermClient
from .types import (DEFAULT_PORTS, DISCOVERY_TIMEOUT, SessionInfo, SessionRecord,
                    _from_dict)


class CodeAPIClient:
    """Typed Python client for the CODEAPI VS Code extension HTTP server."""

    def __init__(self, ports=None, timeout=30.0):
        self.ports = ports or DEFAULT_PORTS
        self.timeout = timeout
        self._sessions: List[SessionRecord] = []
        self.term = TermClient(self)

    # -- raw HTTP --------------------------------------------------------

    def _http(self, method, url, body=None, content_type=None, timeout=None):
        t = timeout if timeout is not None else self.timeout
        data = None
        headers = {}
        public_token = os.environ.get("CODEAPI_PUBLIC_TOKEN")
        if public_token:
            headers["X-CODEAPI-TOKEN"] = public_token
        if body is not None:
            if isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type or "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=t) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except (ValueError, AttributeError):
                pass
            raise CodeAPIError(f"HTTP {e.code} from {url}: {detail}") from e
        except OSError as e:
            raise CodeAPIError(f"Cannot reach {url}: {e}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    # -- session discovery -----------------------------------------------

    def _probe(self, port) -> Optional[SessionRecord]:
        """GET /session on one port; return a SessionRecord, or None if nothing
        CODEAPI-shaped answers there."""
        base_url = f"http://127.0.0.1:{port}"
        try:
            data = self._http("GET", f"{base_url}/session", timeout=DISCOVERY_TIMEOUT)
        except CodeAPIError:
            return None
        if not isinstance(data, dict):
            return None
        return SessionRecord(port=port, info=_from_dict(SessionInfo, data),
                             base_url=base_url)

    def discover(self) -> List[SessionRecord]:
        """Probe every configured port (legacy 3637-3646 + adapter 47337-47400) and
        return the CODEAPI runtimes that answered."""
        self._sessions = [rec for rec in (self._probe(p) for p in self.ports)
                          if rec is not None]
        return self._sessions

    def target(self, session_id=None, workspace=None, port=None,
               runtime_id=None) -> SessionRecord:
        """Resolve exactly ONE target session — strictly, for EVERY selector.

        An explicit ``port`` is authoritative and probed DIRECTLY (even outside the
        default scan bands), so any runtime can be pinned by port. Every OTHER
        selector — ``runtime_id`` (codeapiRuntimeId), ``session_id`` (id prefix),
        ``workspace`` (uri substring), or NO selector at all — resolves through the
        discovery layer's strict selector, which never returns the first of several
        candidates:

            zero matches                          -> TARGET_NOT_FOUND
            exactly one match                     -> selected
            more than one match                   -> TARGET_AMBIGUOUS
            no selector + several live runtimes   -> TARGET_AMBIGUOUS

        This makes the digest's promise ("ambiguity is an error") hold for ordinary
        term/list/write and operations commands too, not just -r. Pin one with
        -r/-s/-w/-p."""
        if port:
            rec = self._probe(port)
            if rec is not None:
                return rec
            raise CodeAPIError(
                f"TARGET_NOT_FOUND: no CODEAPI runtime answered /session on port "
                f"{port}. Is a VS Code window with the CODEAPI extension bound to "
                "that port?")
        from . import discovery
        sessions = discovery.discover_sessions()
        if not sessions:
            raise CodeAPIError(
                "TARGET_NOT_FOUND: no CODEAPI runtimes found on ports 3637-3646 or "
                "47337-47400. Make sure VS Code is running with the CODEAPI extension "
                "enabled.")
        try:
            ds = discovery.select_session(
                sessions, session_id=session_id, workspace=workspace,
                codeapi_runtime_id=runtime_id)
        except ValueError as e:
            msg = str(e)
            code = "TARGET_AMBIGUOUS" if "ambiguous" in msg else "TARGET_NOT_FOUND"
            raise CodeAPIError(f"{code}: {msg}") from e
        return SessionRecord(port=ds.port,
                             info=_from_dict(SessionInfo, ds.raw_session or {}),
                             base_url=ds.base_url)

    def _resolve(self, session_id=None, workspace=None, port=None, runtime_id=None):
        """Unpack targeting kwargs into a SessionRecord."""
        return self.target(session_id=session_id, workspace=workspace, port=port,
                           runtime_id=runtime_id)

    # -- base endpoints --------------------------------------------------

    def health(self, **target_kw) -> dict:
        s = self._resolve(**target_kw)
        return self._http("GET", f"{s.base_url}/health")

    def session(self, **target_kw) -> SessionInfo:
        s = self._resolve(**target_kw)
        data = self._http("GET", f"{s.base_url}/session")
        return _from_dict(SessionInfo, data)

    def exec_js(self, code, **target_kw):
        """POST /exec — run JavaScript in the extension host."""
        s = self._resolve(**target_kw)
        return self._http("POST", f"{s.base_url}/exec",
                          body=code, content_type="text/plain")

    def exec_with_action(self, code, on_result, **target_kw):
        """POST /exec-with-action.

        WARNING: NOT served by the current extension runtime — the live route
        returns 404 (verified, gate wire-probe-014). It exists only in
        standalone-exec-with-action.js (a separate test server on port 4000).
        Kept for parity with the retired seed CLI; do not depend on it against the
        extension. See docs/WIRE_CONTRACT.md.
        """
        s = self._resolve(**target_kw)
        return self._http("POST", f"{s.base_url}/exec-with-action",
                          body={"code": code, "onResult": on_result})

    def command(self, command_id, args=None, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("POST", f"{s.base_url}/command/{command_id}",
                          body={"args": args or []})

    def apis(self, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("GET", f"{s.base_url}/apis")

    def message(self, text, msg_type="info", **target_kw):
        s = self._resolve(**target_kw)
        return self._http("POST", f"{s.base_url}/window/showMessage",
                          body={"message": text, "type": msg_type})

    def workspace_folders(self, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("GET", f"{s.base_url}/workspace/folders")

    def editor_active(self, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("GET", f"{s.base_url}/editor/active")

    def mesh_peers(self, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("GET", f"{s.base_url}/mesh/peers")

    def mesh_broadcast(self, endpoint, body=None, **target_kw):
        s = self._resolve(**target_kw)
        ep = endpoint.lstrip("/")
        return self._http("POST", f"{s.base_url}/mesh/broadcast/{ep}",
                          body=body or {})

    def endpoint_add(self, path, response, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("POST", f"{s.base_url}/add-endpoint",
                          body={"path": path, "response": response})

    def endpoint_remove(self, path, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("POST", f"{s.base_url}/remove-endpoint",
                          body={"path": path})

    def get(self, path, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("GET", f"{s.base_url}{path}")

    def post(self, path, body, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("POST", f"{s.base_url}{path}", body=body)

    def delete(self, path, **target_kw):
        s = self._resolve(**target_kw)
        return self._http("DELETE", f"{s.base_url}{path}")

    def operations(self, operation_id=None, **target_kw):
        """GET /operations (the machine-readable CODEAPI operation digest) or
        GET /operations/:operationId for a single operation definition."""
        s = self._resolve(**target_kw)
        path = "/operations/" + operation_id if operation_id else "/operations"
        return self._http("GET", f"{s.base_url}{path}")

    # -- composite operations -----------------------------------------------

    def verify(self, **target_kw) -> dict:
        """Golden-path preflight: health, session, exec, workspace, terminals."""
        checks = {}
        try:
            s = self._resolve(**target_kw)
            base = s.base_url
            info = s.info
            sid = info.id if isinstance(info, SessionInfo) else info.get("id", "?")
            ws_uri = info.workspaceUri if isinstance(info, SessionInfo) else info.get("workspaceUri")
            checks["session"] = {"ok": True, "id": sid, "port": s.port,
                                 "workspaceUri": ws_uri}
        except CodeAPIError as e:
            checks["session"] = {"ok": False, "error": str(e)}
            return {"ok": False, "checks": checks}

        try:
            self._http("GET", f"{base}/health")
            checks["health"] = {"ok": True}
        except CodeAPIError as e:
            checks["health"] = {"ok": False, "error": str(e)}

        try:
            ws = self._http("POST", f"{base}/exec",
                            body="return vscode.workspace.name",
                            content_type="text/plain")
            checks["workspace"] = {"ok": True, "name": ws}
        except CodeAPIError as e:
            checks["workspace"] = {"ok": False, "error": str(e)}

        try:
            terms = self._http("GET", f"{base}/term/list")
            count = len(terms) if isinstance(terms, list) else 0
            checks["terminals"] = {"ok": True, "count": count, "route": "/term/list"}
        except CodeAPIError as e:
            checks["terminals"] = {"ok": False, "error": str(e)}

        all_ok = all(c.get("ok", False) for c in checks.values())
        return {"ok": all_ok, "checks": checks}
