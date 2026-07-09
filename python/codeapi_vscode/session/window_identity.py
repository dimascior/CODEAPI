"""WindowIdentity — identity fields for the runtime's /session payload.

Phase 11. In the standalone runtime there is no VS Code window, so `window_id`
is 0 (documented placeholder) and `workspace_uri` comes from the CODEAPI_WORKSPACE_URI
env the adapter passes (empty when run bare). `pid` is the runtime process, and
`port` is the bound Python-service port (the /session serverPort).

Adapter-pinned window identity (2026-07-07, additive) — the JS adapter also passes
three window-correlation signals which the runtime echoes verbatim (all `None` in the
standalone / no-adapter runtime):
  - vscode_session_id  <- CODEAPI_VSCODE_SESSION_ID  (vscode.env.sessionId)
  - extension_host_pid <- CODEAPI_EXTENSION_HOST_PID (the adapter's process.pid, NOT ours)
  - codeapi_window_id  <- CODEAPI_WINDOW_ID          (durable workspaceState UUID)
`window_id` (the old placeholder 0) is left unchanged and is NOT repurposed.
"""

import os


class WindowIdentity:
    def __init__(self, pid=None, workspace_uri="", window_id=0, port=None,
                 vscode_session_id=None, extension_host_pid=None,
                 codeapi_window_id=None):
        self.pid = pid if pid is not None else os.getpid()
        self.workspace_uri = workspace_uri or ""
        self.window_id = window_id
        self.port = port
        self.vscode_session_id = vscode_session_id or None
        self.extension_host_pid = extension_host_pid
        self.codeapi_window_id = codeapi_window_id or None

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
        }
