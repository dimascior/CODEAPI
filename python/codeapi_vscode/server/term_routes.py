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

import json as _json
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


# The canonical default for an omitted `enter`. TRUE, matching the CLI
# (enter = not args.no_enter) and TerminalClient.write(enter=True). The
# HTTP route previously reached bool(b.get("enter")) -> None -> False, so
# an omitting HTTP caller got the OPPOSITE of an omitting CLI caller for
# the same payload. Converging on the majority contract.
ENTER_DEFAULT = True


class _FieldError(Exception):
    """A request field or body shape failed validation."""


def _object_body(req):
    """A JSON OBJECT body, or a request error.

    Strict where _json_body is tolerant, and the write route needs it:
    _json_body maps a malformed body AND a non-object root (null, array,
    string, number, bare boolean) to {}. That was survivable while an
    omitted `enter` meant False. It stopped being survivable the moment
    ENTER_DEFAULT became True, because {} then yields data=None with
    enter=True, the extension maps a null payload to '' (termWrite:537),
    and the write becomes a BARE CR - pressing Enter on whatever the
    foreground application is holding.

    So tightening `enter` without tightening the body shape would have
    made an unparseable request MORE dangerous than before. For a PTY
    write, defaulting an unreadable request to "submit a line" is
    indefensible; both cases are refused.

    Parses req.text() DIRECTLY rather than calling req.json(). That is not
    a style preference. req.json() delegates to util.json.loads, which is
    tolerant on purpose - it returns {} for a malformed body and never
    raises - so routing through it makes an unparseable request literally
    indistinguishable from {}. An earlier version of this function wrapped
    req.json() in try/except and reported "request body is not valid
    JSON"; the acceptance matrix measured the response and found that
    branch was DEAD, with malformed bodies falling through to "'data' is
    required" instead. The rejection still happened, but by a DIFFERENT
    guard than the one documented here - so the moment `data` acquired a
    default, malformed input would silently become {} and, under
    ENTER_DEFAULT=True, a bare CR. The strict parse makes the documented
    guard the one that actually fires.
    """
    try:
        data = _json.loads(req.text())
    except Exception as e:  # noqa: BLE001 - malformed body is a client error
        raise _FieldError("request body is not valid JSON: {}".format(e))
    if not isinstance(data, dict):
        raise _FieldError(
            "request body must be a JSON object; got {}".format(
                type(data).__name__
            )
        )
    return data


def _string(body, name):
    """A REQUIRED JSON string, or a request error.

    `data` is required rather than defaulted, because with
    ENTER_DEFAULT=True an omitted payload is not a harmless no-op: the
    extension maps a null payload to '' (termWrite:537) and then appends
    CR, so {} would become pty.write('\\r') - a bare Enter pressed on
    whatever the foreground application is holding. Omission must never
    silently become a terminal control action.

    That leaves intentional empty input fully expressible and clearly
    distinguished:

        {"data":""}                 deliberate bare Enter
        {"data":"","enter":false}   nothing at all
        {"data":"q","enter":false}  exact text, no terminator

    Compatibility: no supported caller omits `data`. The CLI takes `text`
    as a required positional argument, and TerminalClient.write takes
    `data` positionally, so neither can omit it. The extension's
    `opts.data == null ? ''` guard is defensive coding at the transport
    edge, not an API contract - anticipating a null is not the same as
    supporting omission.

    The type is checked HERE rather than relying on the extension's
    String(opts.data), which would otherwise coerce 123 into the text
    "123" and {"x":1} into "[object Object]" and type them into a shell.
    """
    if name not in body:
        raise _FieldError("{!r} is required".format(name))
    value = body[name]
    if not isinstance(value, str):
        raise _FieldError(
            "{!r} must be a JSON string; got {}".format(
                name, type(value).__name__
            )
        )
    return value


def _boolean(body, name, default):
    """A JSON boolean, or a request error - never a truthiness coercion.

    bool(b.get(name)) accepted any JSON value, and the dangerous case was
    not the obvious one: the STRING "false" is truthy, so a caller asking
    for no Enter got an Enter. In a PTY an unintended Enter submits
    whatever is on the current input line, which is the same failure class
    this route's regression fixture documents. Numbers are rejected too -
    0/1 are a C convention, not a JSON boolean, and accepting them invites
    the same class of client-side stringification bug.
    """
    if name not in body:
        return default
    value = body[name]
    if isinstance(value, bool):
        return value
    raise _FieldError(
        "{!r} must be a JSON boolean (true/false); got {}".format(
            name, type(value).__name__
        )
    )


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
        try:
            b = _object_body(req)
            data = _string(b, "data")
            enter = _boolean(b, "enter", ENTER_DEFAULT)
        except _FieldError as e:
            return (400, {"error": str(e)})
        # returns {id,wrote}; `wrote` is created ONLY by the extension as
        # Buffer.byteLength of the final string INCLUDING any appended CR
        # (extension-adapter/extension.js termWrite). Python propagates it
        # verbatim. It is transport acceptance - not command success, not
        # admission, not proof of life.
        return _guarded(lambda: (200, adapter.term_write(
            tid, data, enter=enter)))

    def submit_handler(req):
        # Starting a shell command is NOT the same operation as sending raw
        # input, and they are deliberately different routes. This one is
        # gated on readiness, generation and exclusive ownership; /write
        # stays ungated so a caller can still answer a pager or a REPL.
        tid = req.params.get("id")
        try:
            b = _object_body(req)
            execution_id = _string(b, "executionId")
            data = _string(b, "data")
            expected_generation = _string(b, "expectedGeneration")
        except _FieldError as e:
            return (400, {"error": str(e)})
        return _guarded(lambda: (200, adapter.term_submit(
            tid, execution_id=execution_id, data=data,
            expected_generation=expected_generation)))

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
    router.register_param("POST", "/term/:id/submit", submit_handler)
    router.register_param("GET", "/term/:id/read", read_handler)
    router.register_param("POST", "/term/:id/resize", resize_handler)
    router.register_param("DELETE", "/term/:id", delete_handler)
