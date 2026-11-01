"""WindowIdentity — identity fields for the runtime's /session payload.

Phase 11. In the standalone runtime there is no VS Code window, so `window_id`
is 0 (documented placeholder) and `workspace_uri` comes from the CODEAPI_WORKSPACE_URI
env the adapter passes (empty when run bare). `pid` is the runtime process, and
`port` is the bound Python-service port (the /session serverPort).

Adapter-pinned identity (additive) — the JS adapter passes correlation signals which
the runtime echoes verbatim (all `None` in the standalone / no-adapter runtime):
  - vscode_session_id    <- CODEAPI_VSCODE_SESSION_ID  (vscode.env.sessionId; per-window,
                            session-scoped)
  - extension_host_pid   <- CODEAPI_EXTENSION_HOST_PID (the adapter's process.pid, NOT ours)
  - codeapi_workspace_id <- CODEAPI_WORKSPACE_ID       (durable workspaceState UUID;
                            per-WORKSPACE — shared by windows on one workspace)
  - codeapi_runtime_id   <- CODEAPI_RUNTIME_ID         (fresh UUID per adapter activation;
                            the unique live-window target, never persisted)
  - codeapi_window_id    <- CODEAPI_WINDOW_ID          (LEGACY NAME for the workspace id,
                            kept for compatibility; semantically a workspace identity)
`window_id` (the old placeholder 0) is left unchanged and is NOT repurposed.
"""

import os


class WindowIdentity:
    def __init__(self, pid=None, workspace_uri="", window_id=0, port=None,
                 vscode_session_id=None, extension_host_pid=None,
                 codeapi_window_id=None, codeapi_workspace_id=None,
                 codeapi_runtime_id=None):
        self.pid = pid if pid is not None else os.getpid()
        self.workspace_uri = workspace_uri or ""
        self.window_id = window_id
        self.port = port
        self.vscode_session_id = vscode_session_id or None
        self.extension_host_pid = extension_host_pid
        # The workspace id and its legacy alias resolve to one value: prefer the
        # explicit workspace id, fall back to the legacy window-id name.
        self.codeapi_workspace_id = (codeapi_workspace_id or codeapi_window_id) or None
        self.codeapi_window_id = self.codeapi_workspace_id
        self.codeapi_runtime_id = codeapi_runtime_id or None

    @classmethod
    def from_env(cls, port):
        """Build identity from the launch environment (adapter-provided) + port."""
        ehp = os.environ.get("CODEAPI_EXTENSION_HOST_PID")
        try:
            ehp = int(ehp) if ehp else None
        except ValueError:
            ehp = None
        return cls(
            pid=os.getpid(),
            workspace_uri=os.environ.get("CODEAPI_WORKSPACE_URI", ""),
            window_id=0,  # standalone runtime: no VS Code window (unchanged)
            port=port,
            vscode_session_id=os.environ.get("CODEAPI_VSCODE_SESSION_ID") or None,
            extension_host_pid=ehp,
            codeapi_window_id=os.environ.get("CODEAPI_WINDOW_ID") or None,
            codeapi_workspace_id=os.environ.get("CODEAPI_WORKSPACE_ID") or None,
            codeapi_runtime_id=os.environ.get("CODEAPI_RUNTIME_ID") or None,
        )

    def as_dict(self):
        return {
            "pid": self.pid,
            "workspaceUri": self.workspace_uri or None,
            "windowId": self.window_id,
            "serverPort": self.port,
            "vscodeSessionId": self.vscode_session_id,
            "extensionHostPid": self.extension_host_pid,
            "codeapiWindowId": self.codeapi_window_id,
            "codeapiWorkspaceId": self.codeapi_workspace_id,
            "codeapiRuntimeId": self.codeapi_runtime_id,
        }
