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
        sid="abcd1234", server=None, cwid=None, vsid=None, ehp=None,
        cwsid=None, rtid=None):
    return DiscoveredSession(
        port=port, base_url=f"http://127.0.0.1:{port}", healthy=healthy,
        server_port=server if server is not None else port, workspace_uri=ws,
        pid=1, window_id=0, session_id=sid, terminal_routes=term, runtime_type=rtype,
        codeapi_window_id=cwid, vscode_session_id=vsid, extension_host_pid=ehp,
        # mirror discover_sessions(): workspace id falls back to the legacy name
        codeapi_workspace_id=cwsid if cwsid is not None else cwid,
        codeapi_runtime_id=rtid)


pool = [
    _mk(3637, rtype="legacy", term="absent", sid="leg00001"),
    _mk(47337, rtype="adapter", term="unavailable", sid="ada00001"),
    _mk(47339, rtype="adapter", term="live", sid="d7e53a83"),
    _mk(47340, healthy=False, rtype="adapter", term="absent", sid="dead0001"),
]

# --- select_session (strict: a selector never silently picks one of several) ---
check("select by port", select_session(pool, port=47339).port == 47339)
try:
    select_session(pool, workspace="CODEAPI")
    check("select by shared workspace alone -> ambiguous ValueError", False)
except ValueError as e:
    check("select by shared workspace alone -> ambiguous ValueError",
          "ambiguous" in str(e), str(e))
check("select by workspace + runtime_type narrows to one",
      select_session(pool, workspace="CODEAPI", runtime_type="legacy").port == 3637)
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
try:
    select_session(ipool, extension_host_pid=24104)
    check("select by shared extension_host_pid -> ambiguous ValueError", False)
except ValueError as e:
    check("select by shared extension_host_pid -> ambiguous ValueError",
          "ambiguous" in str(e), str(e))

# --- select_by_identity precedence ---
check("identity precedence: codeapiWindowId resolves",
      select_by_identity(ipool, "win-BBB").port == 47339)
check("identity precedence: vscodeSessionId resolves",
      select_by_identity(ipool, "vs-222").port == 47339)
check("identity precedence: serverPort digit resolves",
      select_by_identity(ipool, "47338").port == 47338)
try:
    select_by_identity(ipool, "CODEAPI")
    check("identity precedence: shared workspace substring -> ambiguous", False)
except ValueError as e:
    check("identity precedence: shared workspace substring -> ambiguous",
          "ambiguous" in str(e), str(e))
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
try:
    select_by_identity(dup, "CODEAPI")
    check("duplicate-workspace: bare workspace token -> ambiguous ValueError", False)
except ValueError as e:
    check("duplicate-workspace: bare workspace token -> ambiguous ValueError",
          "ambiguous" in str(e), str(e))

# --- adversarial: codeapiWindowId COLLISION (same workspace, two windows) ---
# codeapiWindowId lives in workspaceState, which VS Code scopes per WORKSPACE,
# not per window. Two simultaneous windows on the same workspace (e.g.
# "Duplicate Workspace in New Window") share the memento and therefore report
# the SAME codeapiWindowId. The id must then be treated as ambiguous, never
# silently resolved to the first match; vscodeSessionId stays per-window unique.
coll = [
    _mk(47341, cwid="win-SAME", vsid="vs-alpha", ws="file:///CODEAPI", sid="c1"),
    _mk(47342, cwid="win-SAME", vsid="vs-beta", ws="file:///CODEAPI", sid="c2"),
    _mk(47343, cwid="win-OTHER", vsid="vs-gamma", ws="file:///Elsewhere", sid="c3"),
]
mark_duplicates(coll)
check("cwid collision: both sharers flagged duplicate-codeapi-window-id",
      all("duplicate-codeapi-window-id" in s.flags for s in coll[:2]),
      str([s.flags for s in coll]))
check("cwid collision: unique id NOT flagged",
      "duplicate-codeapi-window-id" not in coll[2].flags, str(coll[2].flags))
try:
    select_by_identity(coll, "win-SAME")
    check("cwid collision: select_by_identity raises (no silent first-match)", False)
except ValueError as e:
    check("cwid collision: select_by_identity raises (no silent first-match)",
          "ambiguous" in str(e) and "47341" in str(e) and "47342" in str(e), str(e))
check("cwid collision: vscodeSessionId still targets each window",
      select_by_identity(coll, "vs-alpha").port == 47341
      and select_by_identity(coll, "vs-beta").port == 47342)
check("cwid collision: unique cwid still resolves normally",
      select_by_identity(coll, "win-OTHER").port == 47343)

# --- codeapiRuntimeId: the unique live-window target ---
# THREAT MODEL — first-activation race: two windows activating simultaneously on
# an id-less workspace can mint DIFFERENT workspace ids and race the persist (the
# stored value converges to the last write after reload), OR read the SAME stored
# id. Either way the workspace id is not a unique window target. The runtime id
# is minted fresh per adapter activation and never persisted, so it has no
# shared-storage race: it uniquely targets the live window regardless of what the
# workspace id did.
race = [
    _mk(47344, cwid="win-RACE", rtid="rt-alpha", vsid="vs-a", ws="file:///CODEAPI"),
    _mk(47345, cwid="win-RACE", rtid="rt-beta", vsid="vs-b", ws="file:///CODEAPI"),
]
mark_duplicates(race)
check("runtimeId: targets each window despite colliding workspace id",
      select_by_identity(race, "rt-alpha").port == 47344
      and select_by_identity(race, "rt-beta").port == 47345)
check("runtimeId: precedence beats the colliding workspace-id tier",
      select_by_identity(race, "rt-alpha").port == 47344)  # no ValueError raised
check("select_session by codeapi_runtime_id",
      select_session(race, codeapi_runtime_id="rt-beta").port == 47345)
try:
    select_session(race, codeapi_workspace_id="win-RACE")
    check("workspace_id criteria with two windows -> ambiguous ValueError", False)
except ValueError as e:
    check("workspace_id criteria with two windows -> ambiguous ValueError",
          "ambiguous" in str(e), str(e))

passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"\n{'=' * 44}")
print(f"discovery: {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
