"""Request router for the Tier-2 Python service (Phase 11).

Maps (method, path) -> handler. Supports exact routes, one path-param style route
(/mesh/broadcast/:endpoint), and a dynamic-GET fallback (registered dynamic
endpoints). Anything unmatched returns 404 with a JSON {error} body — the runtime
standardizes on JSON errors while keeping the extension's 404-for-unknown
behavior (see docs/WIRE_CONTRACT.md).

Handlers take a Request and return (status_int, payload) where payload is a
JSON-serializable object (or None for an empty body).
"""

import re
from urllib.parse import unquote

from ..util.json import loads


def _parse_query(qs):
    """Parse a query string into a flat {key: value} dict (last value wins).

    Added for Phase 13D: GET /term/:id/read takes ?tail=N&clear=bool. Kept minimal
    and route-agnostic."""
    out = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        key, _, val = pair.partition("=")
        out[unquote(key)] = unquote(val)
    return out


class Request:
    def __init__(self, method, path, body=b"", params=None, query=None):
        self.method = method
        self.path = path
        self._body = body
        self.params = params or {}
        self.query = query or {}

    def json(self):
        return loads(self._body)

    def text(self):
        """Raw request body as text (for text/plain routes like /exec)."""
        b = self._body
        if isinstance(b, (bytes, bytearray)):
            return b.decode("utf-8")
        return b or ""


class Router:
    def __init__(self):
        self._exact = {}                 # (method, path) -> handler
        self._param = []                 # (method, regex, keys, handler)
        self._dynamic_getter = None      # fn(path) -> payload or None

    def register(self, method, path, handler):
        self._exact[(method, path)] = handler

    def register_param(self, method, template, handler):
        """template like '/mesh/broadcast/:endpoint'."""
        keys = re.findall(r":([A-Za-z_][A-Za-z0-9_]*)", template)
        pattern = "^" + re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"([^/]+)", template) + "$"
        self._param.append((method, re.compile(pattern), keys, handler))

    def set_dynamic_getter(self, fn):
        self._dynamic_getter = fn

    def dispatch(self, method, raw_path, body=b""):
        head, sep, qs = raw_path.partition("?")
        path = head
        query = _parse_query(qs) if sep else {}
        handler = self._exact.get((method, path))
        if handler is not None:
            return handler(Request(method, path, body, query=query))
        for m, regex, keys, phandler in self._param:
            if m != method:
                continue
            match = regex.match(path)
            if match:
                params = dict(zip(keys, match.groups()))
                return phandler(Request(method, path, body, params, query=query))
        if method == "GET" and self._dynamic_getter is not None:
            payload = self._dynamic_getter(path)
            if payload is not None:
                return (200, payload)
        # Out-of-set route: documented 404 (JSON-standardized).
        return (404, {"error": f"Cannot {method} {path}"})
