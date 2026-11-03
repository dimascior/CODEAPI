"""Adapter-backed public routes (Phase 12E).

Delegate to VsCodeAdapter (the JS adapter bridge). PUBLIC wire shaping lives HERE
(docs/ADAPTER_BRIDGE_CONTRACT.md §4):

  POST /exec               unwrapped value; EMPTY body when the JS result was
                           `undefined`; None -> `null`
  POST /command/:id        wrapped {success:true[,result]}; `result` omitted when
                           the command result is `undefined`
  POST /window/showMessage blocking; {success:true} (selected item NOT surfaced)
  GET  /workspace/folders  passthrough of the probed array shape
  GET  /editor/active      passthrough object, or `null` when no active editor
  GET  /apis               passthrough array (window-dependent; no fixed count)

Availability shaping:
  adapter not configured / unreachable / timeout -> 503 {"error":"vscode adapter not available"}
  adapter error envelope                         -> 500 {"error":"<message>"}

/term/* is NOT registered here — Phase 13 (falls through to the router's 404).
"""

from ..vscode.adapter import UNDEFINED, AdapterError, AdapterUnavailable
from .http_server import NO_BODY

_UNAVAILABLE = (503, {"error": "vscode adapter not available"})


def _json_body(req):
    try:
        data = req.json()
    except Exception:  # noqa: BLE001 - tolerate empty/invalid bodies
        return {}
    return data if isinstance(data, dict) else {}


def _present(value):
    """Passthrough helper: map an unexpected UNDEFINED to null (defensive)."""
    return None if value is UNDEFINED else value


def register(router, adapter, *, restricted=True):
    def _guarded(call):
        try:
            return call()
        except AdapterUnavailable:
            return _UNAVAILABLE
        except AdapterError as e:
            return (500, {"error": str(e)})

    def exec_handler(req):
        code = req.text()

        def call():
            value = adapter.exec_js(code)
            # undefined -> empty body; value/None -> unwrapped (None serializes null)
            return (200, NO_BODY) if value is UNDEFINED else (200, value)
        return _guarded(call)

    def command_handler(req):
        command_id = req.params.get("commandId")
        args = _json_body(req).get("args") or []

        def call():
            value = adapter.command(command_id, args)
            if value is UNDEFINED:
                return (200, {"success": True})            # void command
            return (200, {"success": True, "result": value})
        return _guarded(call)

    def workspace_folders_handler(req):
        return _guarded(lambda: (200, _present(adapter.workspace_folders())))

    def editor_active_handler(req):
        return _guarded(lambda: (200, _present(adapter.editor_active())))

    def show_message_handler(req):
        body = _json_body(req)
        message = body.get("message")
        mtype = body.get("type") or "info"

        def call():
            adapter.show_message(message, mtype)   # BLOCKING; result discarded
            return (200, {"success": True})
        return _guarded(call)

    def apis_handler(req):
        return _guarded(lambda: (200, _present(adapter.apis())))

    # ── Route restriction (Step 2) ──
    # /exec runs arbitrary JS with child_process available; /command runs any
    # VS Code command.  Both are disabled by default.  Set
    # CODEAPI_SECURITY_POLICY=open to re-enable (e.g. legacy tooling).
    if restricted:
        def _blocked(req):
            return (403, {"error": "route disabled by security policy"})
        router.register("POST", "/exec", _blocked)
        router.register_param("POST", "/command/:commandId", _blocked)
    else:
        router.register("POST", "/exec", exec_handler)
        router.register_param("POST", "/command/:commandId", command_handler)

    router.register("GET", "/workspace/folders", workspace_folders_handler)
    router.register("GET", "/editor/active", editor_active_handler)
    router.register("POST", "/window/showMessage", show_message_handler)
    router.register("GET", "/apis", apis_handler)
