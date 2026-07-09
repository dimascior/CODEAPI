"""Public /term/* routes (Phase 13D).

Layer-2 public terminal surface. Each route delegates to the Phase-13C
VsCodeAdapter.term_* methods and passes the adapter's values through unchanged
(docs/TERM_CONTRACT.md, docs/CODEAPI_RUNTIME_ARCHITECTURE.md §5). Python NEVER infers
liveness: `alive`/`exitCode` come straight from the adapter; `wrote` is bytes accepted,
not success/liveness; `read` returns the raw ANSI/VT buffer.

Error shaping (architecture §4):
  adapter unconfigured / unreachable        -> 503 {"error":"vscode adapter not available"}
  adapter reports an unknown terminal id     -> 404 {"error":"Terminal <id> not found"}
  any other adapter error                    -> 500 {"error":"<message>"}

The unknown-id case is distinguished by the adapter-surfaced message, which the bridge
contract fixes as "Terminal <id> not found" (kept here so we do not widen scope into
adapter.py just to carry an HTTP status).
"""

import re

from ..vscode.adapter import AdapterError, AdapterUnavailable

_UNAVAILABLE = (503, {"error": "vscode adapter not available"})
_UNKNOWN_TERM = re.compile(r"Terminal .+ not found\Z")


def _json_body(req):
    try:
        data = req.json()
    except Exception:  # noqa: BLE001 - tolerate empty/invalid bodies
        return {}
    return data if isinstance(data, dict) else {}


def register(router, adapter):
    def _guarded(call):
        try:
            return call()
        except AdapterUnavailable:
            return _UNAVAILABLE
        except AdapterError as e:
            msg = str(e)
            if _UNKNOWN_TERM.match(msg):
                return (404, {"error": msg})
            return (500, {"error": msg})

    def list_handler(req):
        # alive/exitCode passed through verbatim — no Python liveness inference.
        return _guarded(lambda: (200, adapter.term_list()))

    def create_handler(req):
        b = _json_body(req)
        return _guarded(lambda: (200, adapter.term_create(
            id=b.get("id"), cwd=b.get("cwd"), shell=b.get("shell"),
            args=b.get("args"), cols=b.get("cols"), rows=b.get("rows"),
            max_bytes=b.get("maxBytes"), show=b.get("show"))))

    def write_handler(req):
        tid = req.params.get("id")
        b = _json_body(req)
        # returns {id,wrote}; wrote is bytes accepted, not success/liveness.
        return _guarded(lambda: (200, adapter.term_write(
            tid, b.get("data"), enter=bool(b.get("enter")))))

    def read_handler(req):
        tid = req.params.get("id")
        raw_tail = req.query.get("tail")
        tail = int(raw_tail) if raw_tail not in (None, "") else None
        clear = req.query.get("clear") in ("true", "1")
        return _guarded(lambda: (200, adapter.term_read(tid, tail=tail, clear=clear)))

    def resize_handler(req):
        tid = req.params.get("id")
        b = _json_body(req)
        return _guarded(lambda: (200, adapter.term_resize(
            tid, b.get("cols"), b.get("rows"))))

    def delete_handler(req):
        tid = req.params.get("id")
        return _guarded(lambda: (200, adapter.term_delete(tid)))

    router.register("GET", "/term/list", list_handler)
    router.register("POST", "/term/create", create_handler)
    router.register_param("POST", "/term/:id/write", write_handler)
    router.register_param("GET", "/term/:id/read", read_handler)
    router.register_param("POST", "/term/:id/resize", resize_handler)
    router.register_param("DELETE", "/term/:id", delete_handler)
