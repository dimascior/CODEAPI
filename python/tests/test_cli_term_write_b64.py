#!/usr/bin/env python3
"""C3 — the CLI `term write` encoded-text boundary.

Drives the REAL cli.cmd_term_write with a recording client, and for every
payload records WHICH LAYER decided: the CLI's own Base64/UTF-8 decoding, or
the terminal client one layer down. `--b64` is base64-encoded UTF-8 TEXT, not
a raw-byte transport, and the difference only shows up at the boundary — so
each case asserts whether client.term.write was reached at all.

    python python/tests/test_cli_term_write_b64.py
"""

import base64
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode import cli                        # noqa: E402
from codeapi_vscode.errors import CodeAPIError        # noqa: E402


class _Term:
    def __init__(self, calls):
        self._calls = calls

    def write(self, tid, text, enter=None, **kw):
        self._calls.append({"id": tid, "text": text, "enter": enter})
        return {"id": tid, "wrote": len(text.encode("utf-8"))}


class RecordingClient:
    def __init__(self):
        self.calls = []
        self.term = _Term(self.calls)


def _args(text, b64=False, no_enter=False):
    return types.SimpleNamespace(
        id="t1", text=text, b64=b64, no_enter=no_enter, json=True,
        session=None, workspace=None, port=None, runtime_id=None)


def check(results, label, cond, detail=""):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def _run(text, b64=False, no_enter=False):
    """Returns (client, error_or_None). The client records what reached it."""
    client = RecordingClient()
    try:
        cli.cmd_term_write(client, _args(text, b64=b64, no_enter=no_enter))
        return client, None
    except Exception as e:  # noqa: BLE001 - the layer that refused is the finding
        return client, e


def main():
    results = []

    # --- accepted encodings: decoded to TEXT, then handed down -------------
    for label, raw in [
        ("ASCII", "q"),
        ("ASCII multi-char", "echo hi"),
        ("multibyte UTF-8 (CJK)", "中"),
        ("multibyte UTF-8 (emoji, astral)", "\U0001f600"),
        ("U+0000 NUL", "\x00"),
        ("ESC control text", "\x1b[A"),
        ("empty payload", ""),
    ]:
        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        client, err = _run(encoded, b64=True)
        got = client.calls[0] if client.calls else None
        check(results, f"--b64 {label}: decoded to exact text, reached client",
              err is None and got is not None and got["text"] == raw
              and got["enter"] is True,
              "sent={!r}".format(got))

    # The CLI decodes to a str and passes a str. It is NOT a raw-byte
    # transport: what travels on is text, and the byte count is recovered
    # downstream by the extension, not carried from here.
    encoded = base64.b64encode("中".encode("utf-8")).decode("ascii")
    client, err = _run(encoded, b64=True)
    sent = client.calls[0]["text"]
    check(results, "--b64 hands DOWN text, not bytes",
          isinstance(sent, str) and len(sent) == 1
          and len(sent.encode("utf-8")) == 3,
          "type={} chars={} bytes={}".format(
              type(sent).__name__, len(sent), len(sent.encode("utf-8"))))

    # --- refused at the CLI, BEFORE any terminal submission ---------------
    #
    # This is the layer question C3 asks. Both of these fail while the
    # terminal is still untouched, which is why they are safe.
    client, err = _run("!!!!not-base64!!!!", b64=True)
    check(results, "--b64 invalid Base64: refused by the CLI, client never called",
          isinstance(err, CodeAPIError) and not client.calls,
          "err={} calls={}".format(type(err).__name__, len(client.calls)))

    # Valid Base64 whose bytes are not valid UTF-8. base64.b64decode accepts
    # it; .decode("utf-8") is what refuses. Terminal untouched either way.
    bad_utf8 = base64.b64encode(b"\xff\xfe").decode("ascii")
    client, err = _run(bad_utf8, b64=True)
    check(results, "--b64 invalid UTF-8: refused BEFORE terminal submission",
          isinstance(err, UnicodeDecodeError) and not client.calls,
          "err={} calls={}".format(type(err).__name__, len(client.calls)))

    # So there is no arbitrary raw-byte primitive through this interface: a
    # byte sequence that is not valid UTF-8 text cannot be delivered at all.
    check(results, "no raw-byte terminal primitive exists on this path",
          not client.calls)

    # --- enter defaults at the CLI surface --------------------------------
    q = base64.b64encode(b"q").decode("ascii")
    client, _ = _run(q, b64=True)
    check(results, "--b64 without --no-enter -> enter=True (CLI default)",
          client.calls[0]["enter"] is True, "sent={!r}".format(client.calls[0]))

    client, _ = _run(q, b64=True, no_enter=True)
    check(results, "--b64 --no-enter -> enter=False (exact text, no CR)",
          client.calls[0]["enter"] is False, "sent={!r}".format(client.calls[0]))

    client, _ = _run("q")
    check(results, "plain text without --no-enter -> enter=True",
          client.calls[0]["text"] == "q" and client.calls[0]["enter"] is True)

    # Whitespace is stripped from the ENCODED form only — the decoded text is
    # untouched, so a payload that is itself whitespace still survives.
    spaced = "  " + base64.b64encode(b" ").decode("ascii") + "\n"
    client, err = _run(spaced, b64=True)
    check(results, "--b64 strips whitespace around the ENCODING, not in the text",
          err is None and client.calls[0]["text"] == " ",
          "sent={!r}".format(client.calls[0] if client.calls else None))

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*54}")
    print(f"cli term write --b64 boundary: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
