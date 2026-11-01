#!/usr/bin/env python3
"""POST /term/:id/write input contract — the strict-validation acceptance.

Runs the REAL Tier-2 server with a RECORDING adapter and asserts, for every
request shape, BOTH halves of the theorem:

    the HTTP status                     (what the caller is told)
    the adapter invocation              (what the terminal is actually asked to do)

Status alone is not sufficient evidence here. The regression this route exists
for (fixtures/regressions/pty-foreground-ownership-20260809.md) was a terminal
state change that transport-level success reported as fine. So every rejection
case asserts adapter call count == 0, and every acceptance case asserts the
EXACT (term_id, data, enter) triple that reached the adapter — `enter` by
identity, so a truthy stand-in cannot pass as True.

    python python/tests/test_term_write_contract.py
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.runtime import create_server                  # noqa: E402
from codeapi_vscode.session.port_allocator import PortAllocator   # noqa: E402
from codeapi_vscode.vscode.adapter import VsCodeAdapter           # noqa: E402


class RecordingAdapter:
    """Records every term_write invocation. Nothing else is exercised here."""

    def __init__(self):
        self.calls = []

    def term_write(self, term_id, data, *, enter):
        # `enter` is REQUIRED and keyword-only, mirroring the real adapter.
        # A default here would let a route-side omission be absorbed as a
        # plausible value instead of raising.
        self.calls.append((term_id, data, enter))
        return {"id": term_id, "wrote": len(data or "")}


ADAPTER = RecordingAdapter()


def _post_raw(base, path, raw):
    """POST an EXACT byte body — no json.dumps between us and the wire.

    Required for the malformed-body cases: anything that round-trips through a
    JSON encoder is by construction well-formed."""
    req = urllib.request.Request(
        base + path, data=raw, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_json(base, path, body):
    return _post_raw(base, path, json.dumps(body).encode("utf-8"))


def check(results, label, cond, detail=""):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def _reject(results, base, label, raw, want_err=None):
    """A rejected request: HTTP 400, {error:...} body, and ZERO terminal action.

    `want_err` names WHICH guard must fire. Status alone would let a
    rejection pass while the guard that was supposed to produce it is dead
    and some later guard catches the case by accident — which is exactly
    what the first run of this matrix found."""
    before = len(ADAPTER.calls)
    st, body = _post_raw(base, "/term/t1/write", raw)
    after = len(ADAPTER.calls)
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    err = (parsed or {}).get("error")
    shaped = isinstance(parsed, dict) and isinstance(err, str)
    ok = (st == 400 and after == before and shaped
          and (want_err is None or want_err in err))
    check(results, label, ok,
          "status={} calls={} err={!r}".format(st, after - before, err))


def _accept(results, base, label, body, want_data, want_enter):
    """An accepted request: HTTP 200, exactly one call, exact (data, enter)."""
    before = len(ADAPTER.calls)
    st, raw = _post_json(base, "/term/t1/write", body)
    made = ADAPTER.calls[before:]
    one = len(made) == 1
    tid, data, enter = made[0] if one else (None, None, None)
    ok = (st == 200 and one and tid == "t1" and data == want_data
          and enter is want_enter)          # identity: no truthy stand-in passes
    check(results, label, ok,
          "status={} calls={} call={!r}".format(st, len(made), made[0] if one else made))


def main():
    results = []
    port = PortAllocator().resolve(None)
    server, _ = create_server("127.0.0.1", port, adapter=ADAPTER)
    threading.Thread(target=server.start, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                if r.status == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)

    try:
        # -- C1.3B  root body: only a JSON OBJECT is a request -------------
        #
        # Every one of these must fail BEFORE reaching ENTER_DEFAULT=True.
        # If a non-object root were coerced to {}, it would inherit that
        # default and a bare CR would be submitted to whatever holds the
        # terminal foreground — a request nobody made.
        print("\n-- C1.3B root-body matrix --")
        _NOT_JSON = "request body is not valid JSON"
        _NOT_OBJECT = "request body must be a JSON object"
        for label, raw, want in [
            ("malformed JSON (truncated object)", b'{"data":"q"', _NOT_JSON),
            ("malformed JSON (trailing comma)", b'{"data":"q",}', _NOT_JSON),
            ("malformed JSON (bare word)", b'not json at all', _NOT_JSON),
            ("no body at all", b'', _NOT_JSON),
            # Body bytes that are not UTF-8 at all: req.text() decodes
            # strictly, so this fails at the same guard rather than being
            # replacement-charactered into something parseable.
            ("invalid UTF-8 body", b'{"data":"\xff\xfe"}', _NOT_JSON),
            ("root null", b'null', _NOT_OBJECT),
            ("root array", b'[]', _NOT_OBJECT),
            ("root string", b'"x"', _NOT_OBJECT),
            ("root number", b'0', _NOT_OBJECT),
            ("root boolean", b'true', _NOT_OBJECT),
        ]:
            _reject(results, base, f"root {label} -> 400, 0 calls", raw, want)

        # -- C1.3C  data: required, and required to be a string ------------
        print("\n-- C1.3C data matrix --")
        for label, body, want in [
            ("{} (empty object)", {}, "'data' is required"),
            ('{"enter":true} (enter without data)', {"enter": True},
             "'data' is required"),
            ('{"data":null}', {"data": None}, "must be a JSON string"),
            ('{"data":0}', {"data": 0}, "must be a JSON string"),
            ('{"data":1}', {"data": 1}, "must be a JSON string"),
            ('{"data":false}', {"data": False}, "must be a JSON string"),
            ('{"data":[]}', {"data": []}, "must be a JSON string"),
            ('{"data":{}}', {"data": {}}, "must be a JSON string"),
        ]:
            _reject(results, base, f"data {label} -> 400, 0 calls",
                    json.dumps(body).encode("utf-8"), want)

        # -- C1.3D  enter: a JSON boolean, or nothing ----------------------
        #
        # The string "false" is the case that matters: it is TRUTHY, so a
        # bool() coercion turns an explicit request for NO Enter into an
        # Enter. Rejection, not coercion.
        print("\n-- C1.3D enter matrix --")
        _accept(results, base, 'enter omitted -> data="q" enter=True (default)',
                {"data": "q"}, "q", True)
        _accept(results, base, 'enter=false -> data="q" enter=False (exact text)',
                {"data": "q", "enter": False}, "q", False)
        _accept(results, base, 'enter=true -> data="q" enter=True',
                {"data": "q", "enter": True}, "q", True)
        for label, value in [
            ("null", None), ('"false"', "false"), ('"true"', "true"),
            ("0", 0), ("1", 1), ("[]", []), ("{}", {}),
        ]:
            _reject(results, base, f"enter={label} -> 400, 0 calls",
                    json.dumps({"data": "q", "enter": value}).encode("utf-8"),
                    "must be a JSON boolean")

        # -- C1.3C/D  missing payload != intentional empty payload ---------
        #
        # The structural proof. {} is refused above; "" is honoured here.
        print("\n-- intentional empty-input cases --")
        _accept(results, base, '{"data":""} -> deliberate bare Enter',
                {"data": ""}, "", True)
        _accept(results, base, '{"data":"","enter":false} -> nothing at all',
                {"data": "", "enter": False}, "", False)

        # The three-way distinction, stated as one assertion over the calls
        # already recorded, so it cannot pass by accident of ordering.
        empty_enter = ("t1", "", True) in ADAPTER.calls
        empty_exact = ("t1", "", False) in ADAPTER.calls
        st_missing, _ = _post_json(base, "/term/t1/write", {})
        check(results, "missing payload != intentional empty payload",
              st_missing == 400 and empty_enter and empty_exact,
              "missing={} empty+enter={} empty+exact={}".format(
                  st_missing, empty_enter, empty_exact))

        # An unparseable body and a well-formed {} are DIFFERENT failures and
        # must be reported by different guards. When they collapse onto one
        # message the body-shape guard is dead and something downstream is
        # catching the case by accident.
        _, bad = _post_raw(base, "/term/t1/write", b'{"data":"q"')
        _, empty = _post_raw(base, "/term/t1/write", b'{}')
        bad_err = json.loads(bad).get("error", "")
        empty_err = json.loads(empty).get("error", "")
        check(results, "unparseable body and {} are distinguishable failures",
              bad_err != empty_err and "not valid JSON" in bad_err,
              "malformed={!r} empty_object={!r}".format(bad_err, empty_err))

        # -- C2  the adapter receives an explicit decision -----------------
        #
        # No bridge is contacted: _call is intercepted, so what is proven
        # here is the SIGNATURE and the WIRE PAYLOAD, not transport.
        print("\n-- C2 adapter explicit-enter contract --")
        sent = []

        class _Capturing(VsCodeAdapter):
            def _call(self, method, path, body=None):
                sent.append((method, path, body))
                return {"id": "t1", "wrote": 0}

        cap = _Capturing(port=None)

        def _raises(fn):
            try:
                fn()
            except TypeError as e:
                return str(e)
            return None

        omitted = _raises(lambda: cap.term_write("t1", "q"))
        check(results, "adapter: omitting enter is no longer a valid call",
              omitted is not None, "TypeError={!r}".format(omitted))

        positional = _raises(lambda: cap.term_write("t1", "q", True))
        check(results, "adapter: enter cannot be passed positionally",
              positional is not None, "TypeError={!r}".format(positional))

        for label, value in [('"false"', "false"), ("0", 0), ("1", 1),
                             ("None", None)]:
            msg = _raises(lambda v=value: cap.term_write("t1", "q", enter=v))
            check(results, f"adapter: enter={label} rejected, not coerced",
                  msg is not None, "TypeError={!r}".format(msg))

        sent.clear()
        cap.term_write("t1", "q", enter=True)
        cap.term_write("t1", "q", enter=False)
        bodies = [b for _, _, b in sent]
        check(results, "adapter wire payload carries a real JSON boolean",
              len(bodies) == 2
              and bodies[0] == {"data": "q", "enter": True}
              and bodies[0]["enter"] is True
              and bodies[1] == {"data": "q", "enter": False}
              and bodies[1]["enter"] is False,
              "bodies={!r}".format(bodies))

        # The route is the one production caller. It must reach the adapter
        # with enter supplied, or the keyword-only signature raises.
        check(results, "the route satisfies the adapter contract for every "
              "accepted request",
              len(ADAPTER.calls) == 5
              and all(isinstance(e, bool) for _, _, e in ADAPTER.calls),
              "calls={!r}".format(ADAPTER.calls))
    finally:
        server.stop()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*54}")
    print(f"term write input contract: {passed}/{total} passed")
    print(f"total adapter invocations across the whole run: {len(ADAPTER.calls)}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
