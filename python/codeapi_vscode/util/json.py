"""JSON + text helpers for the runtime and client.

Phase 11: `now_iso` (timestamp shaping for /health and /session) and a tolerant
`loads` for request bodies. `strip_ansi` is the PTY read cleanup (client side).
"""

import json as _json
import re
from datetime import datetime, timezone

_ANSI_RE = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def now_iso():
    """UTC ISO-8601 with milliseconds and a trailing Z, matching the extension's
    /health and /session timestamp format (e.g. 2026-07-05T02:07:13.133Z)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def loads(data):
    """Parse a JSON request body (bytes or str); {} on empty/invalid."""
    if not data:
        return {}
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    try:
        return _json.loads(data)
    except ValueError:
        return {}


def strip_ansi(text):
    """Remove ANSI/VT escape sequences (PTY read cleanup)."""
    if not text:
        return text
    return _ANSI_RE.sub("", text)
