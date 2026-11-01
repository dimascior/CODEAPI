"""/session and /health routes (Phase 11 — pure Python, no vscode API).

Shapes match docs/WIRE_CONTRACT.md. /health -> {status, timestamp};
/session -> the SessionManager payload (serverPort is the runtime's out-of-band
port, e.g. 47337, NOT a VS Code window-band port).
"""

from ..util.json import now_iso


def register(router, session_manager):
    router.register("GET", "/health",
                    lambda req: (200, {"status": "ok", "timestamp": now_iso()}))
    router.register("GET", "/session",
                    lambda req: (200, session_manager.info()))
