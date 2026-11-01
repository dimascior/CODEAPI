"""VsCodeAdapter — Python client for the minimal JS adapter bridge (Phase 12D).

The ONLY place the Python service talks to the extension host. It speaks the
uniform bridge envelope defined in docs/ADAPTER_BRIDGE_CONTRACT.md:

    success 200    -> { "ok": true, "value": <v> }   value OMITTED when the JS
                                                      result was `undefined`;
                                                      present-as-null for `null`
    error   4xx/5xx -> { "error": "<message>" }

Transport: loopback HTTP (NOT AF_UNIX — unavailable on Windows). Every request
carries the per-launch token as `X-CODEAPI-BRIDGE-TOKEN`; a missing/wrong token is
rejected `403 {"error":"forbidden"}` by the bridge before any vscode.* runs.

Envelope -> Python mapping (the load-bearing distinction 12E depends on):
    {"ok":true,"value":<v>}  -> <v>            (present, may be a dict/list/str/num)
    {"ok":true,"value":null} -> None           (explicit null)
    {"ok":true}              -> UNDEFINED       (value omitted / JS undefined)
    {"error":"..."}          -> raise AdapterError
    unreachable / unconfigured -> raise AdapterUnavailable

Public wrap/unwrap shaping stays OUT of here — it belongs to server/vscode_routes.py
(Phase 12E). This client returns the raw bridge value (or the UNDEFINED sentinel).
"""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from ..errors import AdapterError, CodeAPIError

__all__ = ["VsCodeAdapter", "AdapterError", "AdapterUnavailable", "UNDEFINED"]


class AdapterUnavailable(CodeAPIError):
    """The adapter bridge is unreachable or not configured (Tier 3).

    Distinct from AdapterError (which means the bridge answered with an error
    envelope): 12E maps AdapterUnavailable -> 503 and AdapterError -> 500. Both are
    CodeAPIError subclasses but siblings, so neither catches the other.
    """


class _Undefined:
    """Singleton marking an OMITTED bridge value (JS `undefined`) — distinct from
    `None` (which is an explicit JSON `null`)."""

    __slots__ = ()

    def __repr__(self):
        return "UNDEFINED"

    def __bool__(self):
        return False


#: Sentinel returned when the bridge omits `value` (the JS result was `undefined`).
UNDEFINED = _Undefined()

_DEFAULT = object()  # "timeout not overridden" marker (distinct from an explicit None)


def _error_text(raw, code):
    """Extract an {error} message from a bridge error body, else fall back."""
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict) and "error" in payload:
            return str(payload["error"])
    except ValueError:
        pass
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return text.strip() or f"HTTP {code}"


def _parse_ok(raw):
    """Map a 2xx bridge body to a value / the UNDEFINED sentinel, or raise."""
    if not raw:
        raise AdapterError("empty response from adapter bridge")
    try:
        payload = json.loads(raw)
    except ValueError as e:
        snippet = raw[:120]
        raise AdapterError(f"non-JSON adapter response: {snippet!r}") from e
    if not isinstance(payload, dict):
        raise AdapterError(f"unexpected adapter response: {payload!r}")
    if payload.get("ok") is True:
        # Presence of the "value" key is the undefined-vs-null discriminator.
        return payload["value"] if "value" in payload else UNDEFINED
    if "error" in payload:
        raise AdapterError(str(payload["error"]))
    raise AdapterError(f"unexpected adapter envelope: {payload!r}")


class VsCodeAdapter:
    """Python -> extension-host JS adapter bridge (loopback HTTP client)."""

    def __init__(self, port=None, token=None, host="127.0.0.1",
                 timeout=30.0, message_timeout=None):
        """
        port            adapter bridge port; default from CODEAPI_EXTENSION_PORT.
        token           per-launch token; default from CODEAPI_BRIDGE_TOKEN.
        host            loopback host (default 127.0.0.1).
        timeout         read timeout for normal calls (seconds).
        message_timeout read timeout for show_message; None = disabled (blocking),
                        because the bridge holds the request open until the VS Code
                        notification promise resolves (ADAPTER_BRIDGE_CONTRACT.md §5).
        """
        if port is None:
            env_port = os.environ.get("CODEAPI_EXTENSION_PORT")
            port = int(env_port) if env_port else None
        if token is None:
            token = os.environ.get("CODEAPI_BRIDGE_TOKEN")
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.message_timeout = message_timeout

    # -- transport -------------------------------------------------------

    def _call(self, method, path, body=None, timeout=_DEFAULT):
        if self.port is None:
            raise AdapterUnavailable(
                "adapter bridge not configured (set CODEAPI_EXTENSION_PORT or pass port=)")
        url = f"http://{self.host}:{self.port}{path}"
        # The token rides on EVERY request. Loopback is necessary but not sufficient.
        headers = {"X-CODEAPI-BRIDGE-TOKEN": self.token or ""}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        to = self.timeout if timeout is _DEFAULT else timeout
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:          # bridge answered with an error
            raw = e.read()
            raise AdapterError(_error_text(raw, e.code)) from e
        except (urllib.error.URLError, OSError) as e:  # refused / DNS / timeout
            raise AdapterUnavailable(f"adapter bridge unreachable at {url}: {e}") from e
        return _parse_ok(raw)

    # -- adapter-backed operations ---------------------------------------

    def exec_js(self, code):
        """POST /vscode/exec — eval a JS body in the host. Returns the value, or
        UNDEFINED when the body returned `undefined`."""
        return self._call("POST", "/vscode/exec", {"code": code})

    def command(self, command_id, args=None):
        """POST /vscode/command — vscode.commands.executeCommand. Returns the value,
        or UNDEFINED for a void command."""
        return self._call("POST", "/vscode/command",
                           {"id": command_id, "args": list(args) if args else []})

    def workspace_folders(self):
        """GET /vscode/workspace/folders — list of {uri,name,index}."""
        return self._call("GET", "/vscode/workspace/folders")

    def editor_active(self):
        """GET /vscode/editor/active — the active editor object, or None when there
        is no active editor (the bridge sends value:null)."""
        return self._call("GET", "/vscode/editor/active")

    def show_message(self, message, type="info"):
        """POST /vscode/window/message — BLOCKING until the user dismisses/acts on the
        toast. Uses message_timeout (default None = no read timeout) so a healthy but
        blocking call does not look like a failure. Returns the selected item, or
        UNDEFINED when dismissed with no selection."""
        return self._call("POST", "/vscode/window/message",
                           {"message": message, "type": type},
                           timeout=self.message_timeout)

    def apis(self):
        """GET /vscode/apis — array of API descriptors (window-dependent; no fixed
        count)."""
        return self._call("GET", "/vscode/apis")

    # -- terminal operations (Phase 13C) ---------------------------------
    #
    # Thin passthrough to the bridge's /vscode/term/* routes (docs/TERM_CONTRACT.md).
    # Python NEVER infers liveness: term_list returns `alive`/`exitCode` verbatim, and
    # term_write's `wrote` is bytes accepted — not command success and not proof of
    # life. Same token + {ok,value?}/{error} envelope + 503/500 handling as the rest;
    # an unknown-id bridge 404 surfaces as AdapterError (not AdapterUnavailable).

    def _term_path(self, term_id, sub=None):
        base = "/vscode/term/" + quote(str(term_id), safe="")
        return base + "/" + sub if sub else base

    def term_list(self):
        """GET /vscode/term/list -> [ {id,pid,bytes,alive,exitCode} ] (verbatim)."""
        return self._call("GET", "/vscode/term/list")

    def term_create(self, id=None, cwd=None, shell=None, args=None, cols=None,
                    rows=None, max_bytes=None, show=None):
        """POST /vscode/term/create -> {id,shell,cwd,pid}. Only sends provided opts."""
        body = {}
        if id is not None:
            body["id"] = id
        if cwd is not None:
            body["cwd"] = cwd
        if shell is not None:
            body["shell"] = shell
        if args is not None:
            body["args"] = args
        if cols is not None:
            body["cols"] = cols
        if rows is not None:
            body["rows"] = rows
        if max_bytes is not None:
            body["maxBytes"] = max_bytes
        if show is not None:
            body["show"] = show
        return self._call("POST", "/vscode/term/create", body)

    def term_submit(self, term_id, *, execution_id, data, expected_generation):
        """POST /vscode/term/:id/submit — atomic reserve AND write.

        The ONLY path for starting a new shell command. A separate reserve
        followed by a write would leave a window in which something else could
        write first: ownership would prove the caller held the lock, not that
        its bytes were next. The extension does the eligibility check, the
        reservation and the pty.write in one event-loop turn.

        execution_id is CORRELATION, not authority — proposing one grants
        nothing. Authority is the extension's own activeExecutionId.
        """
        return self._call("POST", self._term_path(term_id, "submit"),
                          {"executionId": execution_id, "data": data,
                           "expectedGeneration": expected_generation})

    def term_write(self, term_id, data, *, enter):
        """POST /vscode/term/:id/write -> {id,wrote}. `wrote` = BYTES ACCEPTED, not
        command success and not proof of life.

        `enter` is REQUIRED and must be a real bool. It used to default to
        False, which put a THIRD answer in the tree: the CLI defaulted to
        True (enter = not args.no_enter), TerminalClient.write defaulted to
        True, and this defaulted to False — so the same payload meant
        different things depending on how deep it entered. Defaults belong
        at the semantic surface where a caller expressed an intent; a
        transport edge inventing one is how "no Enter" and "Enter" stopped
        being distinguishable.

        The bool check is deliberate rather than bool(enter): the string
        "false" is truthy, so coercion turns an explicit request for no
        Enter into an Enter — a submitted line in whatever holds the
        terminal foreground.
        """
        if not isinstance(enter, bool):
            raise TypeError(
                "term_write(enter=) must be a bool; got {}".format(
                    type(enter).__name__))
        return self._call("POST", self._term_path(term_id, "write"),
                           {"data": data, "enter": enter})

    def term_read(self, term_id, tail=None, clear=False):
        """GET /vscode/term/:id/read -> {id,bytes,buffer}. buffer is raw ANSI/VT."""
        query = {}
        if tail is not None:
            query["tail"] = tail
        if clear:
            query["clear"] = "true"
        path = self._term_path(term_id, "read")
        if query:
            path += "?" + urlencode(query)
        return self._call("GET", path)

    def term_resize(self, term_id, cols, rows):
        """POST /vscode/term/:id/resize -> {id,cols,rows}."""
        return self._call("POST", self._term_path(term_id, "resize"),
                           {"cols": cols, "rows": rows})

    def term_delete(self, term_id):
        """DELETE /vscode/term/:id -> {id,killed}. Cleanup/removal (TERM_CONTRACT §4)."""
        return self._call("DELETE", self._term_path(term_id))
