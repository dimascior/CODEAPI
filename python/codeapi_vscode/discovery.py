"""codeapi_vscode.discovery — window/session discovery + terminal routing.

Terminal-first control plane for the post-Phase-14 world where several CODEAPI
runtimes can coexist:

  - the **legacy** sourceless bundle answers in the VS Code window band 3637-3646;
  - the **adapter/Python** runtime (root cutover / installed VSIX / dev host) answers
    in the reserved band 47337-47400, outside the window band.

Because a stale/dev/legacy runtime can still return ``/health 200``, health alone is
NOT identity. This module scans BOTH ranges, reads ``/session`` on every responder,
probes for the public ``/term/*`` routes, classifies each runtime, and marks
stale/duplicate/unhealthy sessions so a caller can pick the RIGHT window before
routing a terminal to it.

Terminal routing uses the **public ``/term/*`` routes** (Phase 13D), not the legacy
``codeApiTerm``-over-``/exec`` path — liveness is observed from ``/term/list``
(``alive:false`` + integer ``exitCode``), never inferred from ``DELETE``.

Stdlib only (urllib). Pure classification helpers (``classify_runtime``,
``compute_flags``, ``mark_duplicates``) are network-free and unit-tested in
python/tests/test_discovery.py.
"""

import json
import time
import urllib.error
import urllib.request
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# The two transition ranges. Legacy = the VS Code window band; adapter = the
# reserved Python-service band the runtime binds after the cutover.
LEGACY_RANGE = range(3637, 3647)        # 3637-3646
ADAPTER_RANGE = range(47337, 47401)     # 47337-47400
DEFAULT_RANGES = (LEGACY_RANGE, ADAPTER_RANGE)
DISCOVERY_TIMEOUT = 1.0


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredSession:
    """One responder found during discovery, with identity + classification."""
    port: int                              # the port it actually answered on
    base_url: str
    healthy: bool                          # /health 200 + status ok
    server_port: Optional[int] = None      # /session.serverPort (should == port)
    workspace_uri: Optional[str] = None
    pid: Optional[int] = None
    window_id: Optional[int] = None
    session_id: Optional[str] = None
    # Adapter-pinned window identity (additive; None on pre-identity / standalone runtimes).
    vscode_session_id: Optional[str] = None
    extension_host_pid: Optional[int] = None
    codeapi_window_id: Optional[str] = None
    terminal_routes: str = "absent"        # "live" | "unavailable" | "absent"
    runtime_type: str = "unknown"          # "adapter" | "legacy" | "unknown"
    flags: List[str] = field(default_factory=list)
    raw_session: Optional[dict] = None

    @property
    def stale(self) -> bool:
        """A best-effort 'do not target this' signal from observable evidence."""
        return (not self.healthy) or ("port-mismatch" in self.flags) \
            or ("no-session" in self.flags)

    @property
    def terminals_usable(self) -> bool:
        return self.terminal_routes == "live"


# ---------------------------------------------------------------------------
# Pure classification helpers (no network — unit-tested)
# ---------------------------------------------------------------------------

def classify_runtime(port: int, session: Optional[dict], terminal_routes: str) -> str:
    """Classify a responder as 'adapter' / 'legacy' / 'unknown'.

    The public ``/term/*`` routes are an adapter/Python-runtime feature the legacy
    bundle never had, so their presence is the strongest signal; the port band is
    the tie-breaker.
    """
    if terminal_routes in ("live", "unavailable"):
        return "adapter"
    if port in ADAPTER_RANGE:
        return "adapter"                    # reserved python band
    if port in LEGACY_RANGE:
        return "legacy"                     # window band, no /term/* routes
    return "unknown"


def compute_flags(port: int, healthy: bool, session: Optional[dict],
                  terminal_routes: str, runtime_type: str = "unknown") -> List[str]:
    """Observable per-session flags. 'duplicate-workspace' is added later by
    mark_duplicates once the whole set is known."""
    flags: List[str] = []
    if not healthy:
        flags.append("unhealthy")
    if not session:
        flags.append("no-session")
    else:
        sp = session.get("serverPort")
        if sp is not None and sp != port:
            flags.append("port-mismatch")   # phantom/misconfigured discovery risk
        if not session.get("workspaceUri"):
            flags.append("no-workspace")
        # Legacy windowId placeholder: kept for continuity, NOT relied on for identity.
        if not session.get("windowId"):
            flags.append("no-window-id")
        # The durable identity gap: an adapter runtime that did not report codeapiWindowId
        # (pre-identity build, or launched without the new env). This is the flag to act on.
        if runtime_type == "adapter" and not session.get("codeapiWindowId"):
            flags.append("no-codeapi-window-id")
    if terminal_routes == "unavailable":
        flags.append("no-terminals")         # adapter present but bridge unreachable
    return flags


def mark_duplicates(sessions: List[DiscoveredSession]) -> List[DiscoveredSession]:
    """Flag every session that shares a workspaceUri with another responder."""
    by_ws: Dict[str, List[DiscoveredSession]] = {}
    for s in sessions:
        if s.workspace_uri:
            by_ws.setdefault(s.workspace_uri, []).append(s)
    for group in by_ws.values():
        if len(group) > 1:
            for s in group:
                if "duplicate-workspace" not in s.flags:
                    s.flags.append("duplicate-workspace")
    return sessions


# ---------------------------------------------------------------------------
# HTTP helper (returns status, so 404 vs 503 is distinguishable)
# ---------------------------------------------------------------------------

def _request(method, url, body=None, timeout=DISCOVERY_TIMEOUT):
    """Return (status_or_None, parsed_body). status is None if unreachable."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except (urllib.error.URLError, OSError):
        return None, None
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def _probe_terminal_routes(base_url, timeout=DISCOVERY_TIMEOUT) -> str:
    """GET /term/list and map the status to a route-availability class."""
    status, _ = _request("GET", f"{base_url}/term/list", timeout=timeout)
    if status == 200:
        return "live"           # adapter wired, terminals usable
    if status == 503:
        return "unavailable"    # adapter route exists but bridge unreachable
    return "absent"             # 404 / no responder -> route not present (legacy)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _iter_ports(ranges):
    seen = set()
    for r in ranges:
        for p in r:
            if p not in seen:
                seen.add(p)
                yield p


def discover_sessions(ranges=DEFAULT_RANGES, timeout=DISCOVERY_TIMEOUT,
                      probe_terminals=True) -> List[DiscoveredSession]:
    """Scan the given port ranges; for every responder read /health + /session,
    probe /term/*, classify, and flag. Only ports with a CODEAPI-shaped /session
    (a dict carrying serverPort) OR a healthy /health are reported."""
    out: List[DiscoveredSession] = []
    for port in _iter_ports(ranges):
        base = f"http://127.0.0.1:{port}"
        h_status, h_body = _request("GET", f"{base}/health", timeout=timeout)
        s_status, s_body = _request("GET", f"{base}/session", timeout=timeout)
        # Nothing answered either route -> no responder here.
        if h_status is None and s_status is None:
            continue
        session = s_body if isinstance(s_body, dict) else None
        # Guard against a random non-CODEAPI service occupying the port.
        if session is None and not (isinstance(h_body, dict)
                                    and h_body.get("status") == "ok"):
            continue
        healthy = (h_status == 200 and isinstance(h_body, dict)
                   and h_body.get("status") == "ok")
        term = _probe_terminal_routes(base, timeout) if probe_terminals else "absent"
        rtype = classify_runtime(port, session, term)
        rec = DiscoveredSession(
            port=port, base_url=base, healthy=healthy,
            server_port=(session or {}).get("serverPort"),
            workspace_uri=(session or {}).get("workspaceUri"),
            pid=(session or {}).get("pid"),
            window_id=(session or {}).get("windowId"),
            session_id=(session or {}).get("id"),
            vscode_session_id=(session or {}).get("vscodeSessionId"),
            extension_host_pid=(session or {}).get("extensionHostPid"),
            codeapi_window_id=(session or {}).get("codeapiWindowId"),
            terminal_routes=term,
            runtime_type=rtype,
            flags=compute_flags(port, healthy, session, term, rtype),
            raw_session=session,
        )
        out.append(rec)
    mark_duplicates(out)
    return out


# Identity precedence for resolving a target window (most durable first).
IDENTITY_PRECEDENCE = ("codeapiWindowId", "vscodeSessionId", "extensionHostPid",
                       "workspaceUri", "serverPort")


def select_session(sessions: List[DiscoveredSession], *, port=None, session_id=None,
                   workspace=None, runtime_type=None, codeapi_window_id=None,
                   vscode_session_id=None, extension_host_pid=None,
                   require_terminals=False) -> DiscoveredSession:
    """Pick one session by the given criteria (ANDed). Raises ValueError if none match."""
    cand = [s for s in sessions if s.healthy]
    if require_terminals:
        cand = [s for s in cand if s.terminals_usable]
    if runtime_type:
        cand = [s for s in cand if s.runtime_type == runtime_type]
    if codeapi_window_id:
        cand = [s for s in cand if s.codeapi_window_id == codeapi_window_id]
    if vscode_session_id:
        cand = [s for s in cand if s.vscode_session_id == vscode_session_id]
    if extension_host_pid is not None:
        cand = [s for s in cand if s.extension_host_pid == extension_host_pid]
    if port is not None:
        cand = [s for s in cand if s.port == port or s.server_port == port]
    if session_id:
        cand = [s for s in cand if (s.session_id or "").startswith(session_id)]
    if workspace:
        cand = [s for s in cand if workspace in (s.workspace_uri or "")]
    if not cand:
        raise ValueError(
            "no session matched "
            f"(port={port}, session_id={session_id}, workspace={workspace}, "
            f"runtime_type={runtime_type}, codeapi_window_id={codeapi_window_id}, "
            f"vscode_session_id={vscode_session_id}, extension_host_pid={extension_host_pid}, "
            f"require_terminals={require_terminals})")
    return cand[0]


def select_by_identity(sessions: List[DiscoveredSession], identity,
                       require_terminals=False) -> DiscoveredSession:
    """Resolve a single session from one opaque identity token, trying fields in
    precedence order (see IDENTITY_PRECEDENCE): codeapiWindowId > vscodeSessionId >
    extensionHostPid > workspaceUri (substring) > serverPort. The durable
    codeapiWindowId wins over the ambiguous workspaceUri."""
    cand = [s for s in sessions if s.healthy]
    if require_terminals:
        cand = [s for s in cand if s.terminals_usable]
    ident = str(identity)
    for s in cand:
        if s.codeapi_window_id and s.codeapi_window_id == ident:
            return s
    for s in cand:
        if s.vscode_session_id and s.vscode_session_id == ident:
            return s
    if ident.isdigit():
        for s in cand:
            if s.extension_host_pid == int(ident):
                return s
    for s in cand:
        if ident in (s.workspace_uri or ""):
            return s
    if ident.isdigit():
        for s in cand:
            if s.server_port == int(ident) or s.port == int(ident):
                return s
    raise ValueError(f"no session matched identity {identity!r}")


# ---------------------------------------------------------------------------
# Terminal routing over the PUBLIC /term/* routes
# ---------------------------------------------------------------------------

class TerminalRoutingError(Exception):
    pass


class TerminalRouter:
    """Create/control an adapter-owned terminal in a selected window via /term/*.

    Liveness is observed from /term/list (``alive:false`` + integer ``exitCode``),
    NEVER inferred from DELETE.
    """

    def __init__(self, base_url, timeout=10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _req(self, method, path, body=None):
        return _request(method, f"{self.base_url}{path}", body=body,
                        timeout=self.timeout)

    def create(self, shell="powershell.exe", show=True, **opts) -> dict:
        payload = {"shell": shell, "show": show}
        payload.update(opts)
        status, body = self._req("POST", "/term/create", payload)
        if status != 200 or not isinstance(body, dict):
            raise TerminalRoutingError(f"/term/create -> {status}: {body!r}")
        return body

    def list(self) -> list:
        status, body = self._req("GET", "/term/list")
        if status != 200 or not isinstance(body, list):
            raise TerminalRoutingError(f"/term/list -> {status}: {body!r}")
        return body

    def write(self, term_id, data, enter=True) -> dict:
        status, body = self._req("POST", f"/term/{term_id}/write",
                                 {"data": data, "enter": enter})
        if status != 200 or not isinstance(body, dict):
            raise TerminalRoutingError(f"/term/{term_id}/write -> {status}: {body!r}")
        return body

    def read(self, term_id, tail=None, clear=False):
        q = []
        if tail is not None:
            q.append(f"tail={int(tail)}")
        if clear:
            q.append("clear=true")
        path = f"/term/{term_id}/read" + (("?" + "&".join(q)) if q else "")
        return self._req("GET", path)          # returns (status, body)

    def delete(self, term_id):
        return self._req("DELETE", f"/term/{term_id}")   # (status, body)

    def find(self, term_id) -> Optional[dict]:
        for rec in self.list():
            if isinstance(rec, dict) and rec.get("id") == term_id:
                return rec
        return None

    def wait_exit(self, term_id, timeout=25.0, poll=0.5) -> Optional[dict]:
        """Poll /term/list until the terminal reports alive:false AND an integer
        exitCode (node-pty onExit is the source of truth; the adapter's
        process-existence fallback can flip alive:false a beat earlier)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = self.find(term_id)
            if rec and rec.get("alive") is False and isinstance(rec.get("exitCode"), int):
                return rec
            time.sleep(poll)
        return None

    def run_and_reap(self, shell="powershell.exe", marker=None,
                     exit_timeout=25.0) -> dict:
        """Full terminal-routing proof against the selected window:
        create -> write echo marker -> read marker back -> write exit ->
        observe alive:false + exitCode (via list, NOT delete) -> DELETE ->
        confirm read-after-delete is 404. Returns a structured result dict."""
        marker = marker or ("CODEAPI_ROUTE_" + _uuid.uuid4().hex[:8])
        result = {"marker": marker, "steps": {}, "ok": False}

        created = self.create(shell=shell, show=True)
        tid = created.get("id")
        result["terminal"] = created
        result["steps"]["create"] = bool(tid) and isinstance(created.get("pid"), int)

        alive0 = self.find(tid)
        result["steps"]["alive_true"] = bool(alive0) and alive0.get("alive") is True \
            and alive0.get("exitCode") is None and isinstance(alive0.get("pid"), int)

        self.write(tid, f"echo {marker}", enter=True)
        time.sleep(1.6)
        r_status, r_body = self.read(tid)
        buf = r_body.get("buffer", "") if isinstance(r_body, dict) else ""
        result["steps"]["marker_readback"] = marker in buf

        self.write(tid, "exit", enter=True)
        dead = self.wait_exit(tid, timeout=exit_timeout)
        result["dead_record"] = dead
        result["steps"]["exit_observed"] = dead is not None
        result["exit_code"] = dead.get("exitCode") if dead else None

        d_status, d_body = self.delete(tid)
        result["steps"]["delete_ok"] = d_status == 200
        result["delete"] = d_body

        ra_status, _ = self.read(tid)
        result["steps"]["read_after_delete_404"] = ra_status == 404

        result["ok"] = all(result["steps"].values())
        return result
