"""SessionManager — produces the runtime's /session payload.

Phase 11. Shapes /session byte-compatibly with the extension (see
docs/WIRE_CONTRACT.md): id, pid, workspaceUri, windowId, capabilities, lastSeen,
serverPort, meshPeers, plus the additive adapter-pinned identity fields
vscodeSessionId, extensionHostPid, codeapiWindowId (2026-07-07). `id` is generated
once per runtime process (stable for its lifetime); `lastSeen` is regenerated per
call. `serverPort` is the runtime's bound port (a Python-service port, e.g. 47337 —
NOT a VS Code window-band port).
"""

import uuid

from ..util.json import now_iso


class SessionManager:
    def __init__(self, identity):
        self.identity = identity
        self.id = str(uuid.uuid4())

    def info(self):
        d = self.identity.as_dict()
        # Key order matches docs/WIRE_CONTRACT.md's /session example. The three
        # adapter-pinned identity fields are appended AFTER meshPeers (additive;
        # existing fields/positions unchanged).
        return {
            "id": self.id,
            "pid": d["pid"],
            "workspaceUri": d["workspaceUri"],
            "windowId": d["windowId"],
            "capabilities": [],
            "lastSeen": now_iso(),
            "serverPort": d["serverPort"],
            "meshPeers": [],
            "vscodeSessionId": d["vscodeSessionId"],
            "extensionHostPid": d["extensionHostPid"],
            "codeapiWindowId": d["codeapiWindowId"],
            "codeapiWorkspaceId": d["codeapiWorkspaceId"],
            "codeapiRuntimeId": d["codeapiRuntimeId"],
        }

    def touch(self):
        # lastSeen is computed per info() call; nothing to persist.
        return self.info()
