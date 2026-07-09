#!/usr/bin/env python3
"""Network-free unit tests for codeapi_vscode.discovery classification/selection.

    python python/tests/test_discovery.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(HERE)                 # .../python
sys.path.insert(0, PKG_PARENT)

from codeapi_vscode.discovery import (  # noqa: E402
    DiscoveredSession, classify_runtime, compute_flags, mark_duplicates,
    select_by_identity, select_session)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'OK  ' if ok else 'FAIL'}  {name}  {detail}")


# --- classify_runtime ---
check("classify legacy port + absent -> legacy",
      classify_runtime(3637, {"serverPort": 3637}, "absent") == "legacy")
check("classify legacy port + live -> adapter (routes override)",
      classify_runtime(3639, {"serverPort": 3639}, "live") == "adapter")
check("classify adapter port + live -> adapter",
      classify_runtime(47339, {"serverPort": 47339}, "live") == "adapter")
check("classify adapter port + absent -> adapter (reserved band)",
      classify_runtime(47337, {"serverPort": 47337}, "absent") == "adapter")
check("classify adapter port + unavailable -> adapter",
      classify_runtime(47355, None, "unavailable") == "adapter")
check("classify out-of-band + absent -> unknown",
      classify_runtime(8080, {"serverPort": 8080}, "absent") == "unknown")

# --- compute_flags ---
f = compute_flags(47339, True, {"serverPort": 47339, "workspaceUri": "file:///x",
                                 "windowId": 0}, "live")
check("flags: healthy adapter windowId 0 -> only no-window-id",
      f == ["no-window-id"], str(f))
f = compute_flags(47338, True, {"serverPort": 47337, "workspaceUri": "file:///x",
                                 "windowId": 5}, "live")
check("flags: serverPort != port -> port-mismatch", "port-mismatch" in f, str(f))
f = compute_flags(3637, False, None, "absent")
check("flags: no session + unhealthy", "no-session" in f and "unhealthy" in f, str(f))
f = compute_flags(47355, True, {"serverPort": 47355, "workspaceUri": "file:///x",
                                 "windowId": 1}, "unavailable")
check("flags: terminal unavailable -> no-terminals", f == ["no-terminals"], str(f))
f = compute_flags(47337, True, {"serverPort": 47337, "windowId": 1}, "live")
check("flags: missing workspaceUri -> no-workspace", "no-workspace" in f, str(f))

# --- mark_duplicates ---
a = DiscoveredSession(port=47337, base_url="", healthy=True, workspace_uri="file:///w")
b = DiscoveredSession(port=47338, base_url="", healthy=True, workspace_uri="file:///w")
c = DiscoveredSession(port=3637, base_url="", healthy=True, workspace_uri="file:///other")
mark_duplicates([a, b, c])
check("duplicates: shared workspace flagged on both",
      "duplicate-workspace" in a.flags and "duplicate-workspace" in b.flags)
check("duplicates: unique workspace not flagged",
      "duplicate-workspace" not in c.flags)


def _mk(port, healthy=True, rtype="adapter", term="live", ws="file:///CODEAPI",
        sid="abcd1234", server=None, cwid=None, vsid=None, ehp=None):
    return DiscoveredSession(
        port=port, base_url=f"http://127.0.0.1:{port}", healthy=healthy,
        server_port=server if server is not None else port, workspace_uri=ws,
        pid=1, window_id=0, session_id=sid, terminal_routes=term, runtime_type=rtype,
        codeapi_window_id=cwid, vscode_session_id=vsid, extension_host_pid=ehp)


pool = [
    _mk(3637, rtype="legacy", term="absent", sid="leg00001"),
    _mk(47337, rtype="adapter", term="unavailable", sid="ada00001"),
    _mk(47339, rtype="adapter", term="live", sid="d7e53a83"),
    _mk(47340, healthy=False, rtype="adapter", term="absent", sid="dead0001"),
]

# --- select_session ---
check("select by port", select_session(pool, port=47339).port == 47339)
check("select by workspace substring",
      select_session(pool, workspace="CODEAPI").workspace_uri.endswith("CODEAPI"))
check("select by session prefix", select_session(pool, session_id="d7e5").port == 47339)
check("select by runtime_type legacy", select_session(pool, runtime_type="legacy").port == 3637)
check("select require_terminals -> only live",
      select_session(pool, require_terminals=True).port == 47339)
check("select excludes unhealthy", all(
    select_session(pool, require_terminals=True).port != 47340 for _ in [0]))
try:
    select_session(pool, port=59999)
    check("select no-match raises ValueError", False)
except ValueError:
    check("select no-match raises ValueError", True)
try:
    select_session([_mk(47337, term="unavailable")], require_terminals=True)
    check("select require_terminals with none -> ValueError", False)
except ValueError:
    check("select require_terminals with none -> ValueError", True)

# --- adapter-pinned window identity: flags ---
f = compute_flags(47339, True, {"serverPort": 47339, "workspaceUri": "file:///x",
                                 "windowId": 0}, "live", "adapter")
check("flags: adapter without codeapiWindowId -> no-codeapi-window-id",
      "no-codeapi-window-id" in f, str(f))
f = compute_flags(47339, True, {"serverPort": 47339, "workspaceUri": "file:///x",
                                 "windowId": 0, "codeapiWindowId": "w-123"}, "live", "adapter")
check("flags: adapter WITH codeapiWindowId -> not flagged",
      "no-codeapi-window-id" not in f, str(f))
f = compute_flags(3637, True, {"serverPort": 3637, "workspaceUri": "file:///x",
                               "windowId": 5}, "absent", "legacy")
check("flags: legacy lacking codeapiWindowId -> NOT flagged (adapter-only)",
      "no-codeapi-window-id" not in f, str(f))

# --- identity-aware selection ---
ipool = [
    _mk(47338, cwid="win-AAA", vsid="vs-111", ehp=24104, ws="file:///CODEAPI", sid="s1"),
    _mk(47339, cwid="win-BBB", vsid="vs-222", ehp=24104, ws="file:///CODEAPI", sid="s2"),
]
check("select by codeapi_window_id",
      select_session(ipool, codeapi_window_id="win-BBB").port == 47339)
check("select by vscode_session_id",
      select_session(ipool, vscode_session_id="vs-111").port == 47338)
check("select by extension_host_pid (shared -> first)",
      select_session(ipool, extension_host_pid=24104).port == 47338)

# --- select_by_identity precedence ---
check("identity precedence: codeapiWindowId resolves",
      select_by_identity(ipool, "win-BBB").port == 47339)
check("identity precedence: vscodeSessionId resolves",
      select_by_identity(ipool, "vs-222").port == 47339)
check("identity precedence: serverPort digit resolves",
      select_by_identity(ipool, "47338").port == 47338)
check("identity precedence: workspace substring resolves (first)",
      select_by_identity(ipool, "CODEAPI").port == 47338)
# codeapiWindowId beats a workspace-substring collision: identity 'win-AAA' must not be
# shadowed by the shared workspace.
check("identity precedence: codeapiWindowId wins over workspace",
      select_by_identity(ipool, "win-AAA").port == 47338)
try:
    select_by_identity(ipool, "nonexistent-xyz")
    check("identity no-match raises ValueError", False)
except ValueError:
    check("identity no-match raises ValueError", True)

# --- duplicate-workspace safety: identity beats a shared workspaceUri ---
# Two live adapter sessions on ONE workspace. Selecting by workspace (or "first
# healthy port") is ambiguous; only codeapiWindowId targets a specific window.
dup = [
    _mk(47337, cwid="win-AAA", vsid="vs-1", ws="file:///CODEAPI", sid="s1"),
    _mk(47338, cwid="win-BBB", vsid="vs-2", ws="file:///CODEAPI", sid="s2"),
]
mark_duplicates(dup)
check("duplicate-workspace: both sessions flagged",
      all("duplicate-workspace" in d.flags for d in dup))
check("duplicate-workspace: codeapiWindowId disambiguates -> 47338",
      select_by_identity(dup, "win-BBB").port == 47338)
check("duplicate-workspace: bare workspace token is ambiguous (first match)",
      select_by_identity(dup, "CODEAPI").port == 47337)

passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"\n{'=' * 44}")
print(f"discovery: {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
