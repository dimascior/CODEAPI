"""Gate verification for terminal write operations.

Fails closed: a write is refused unless an inflight gate positively matches
the terminal ID, data hash, and enter flag, has not expired, and has not
been consumed by a prior write this session.  The only pass-through is when
*inflight_dir* is empty (guard unconfigured).

Gate files are NEVER moved or deleted by CODEAPI — only the hook and the
lifecycle tooling manage gate file placement.  Files are read as utf-8-sig
to tolerate the BOM that PowerShell 5.1 ``Set-Content -Encoding UTF8``
produces.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

_consumed = set()


def _parse_utc(text):
    """Parse an ISO-ish UTC timestamp to an aware datetime, or None.

    Python 3.8's ``fromisoformat`` does not accept the ``Z`` suffix,
    so normalise it first.  A naive result is treated as UTC.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        normalised = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def verify_write_gate(inflight_dir, terminal_id, data_text, enter):
    """Check that an unconsumed, unexpired inflight gate matches this write.

    Returns ``(True, correlation_id)`` on a positive match — the gate is
    consumed and will not match again.  Returns ``(False, reason)`` on
    every other outcome; the reason string is safe for the HTTP body.

    Passes unconditionally only when *inflight_dir* is falsy (unconfigured).
    """
    if not inflight_dir:
        return (True, None)

    if not os.path.isdir(inflight_dir):
        return (False, "inflight directory does not exist")

    data_hash = hashlib.sha256(data_text.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)

    try:
        entries = os.listdir(inflight_dir)
    except OSError:
        return (False, "cannot read inflight directory")

    for name in entries:
        if not name.endswith(".gate.json"):
            continue

        gate_key = (inflight_dir, name)
        if gate_key in _consumed:
            continue

        path = os.path.join(inflight_dir, name)
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                gate = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue

        if gate.get("data_sha256") != data_hash:
            continue

        if gate.get("terminal_id") != terminal_id:
            continue

        if gate.get("enter") != enter:
            continue

        expires_text = gate.get("expires_utc")
        if expires_text is not None:
            expires = _parse_utc(expires_text)
            if expires is None:
                continue
            if now >= expires:
                continue

        _consumed.add(gate_key)
        return (True, gate.get("correlation_id"))

    return (False, "no admitted gate matches this write payload")


def verify_submit_gate(inflight_dir, terminal_id, command_text):
    """Check that an unconsumed, unexpired inflight gate matches this submit.

    Matches on ``submit_command_sha256`` (hash of the raw command) and
    ``terminal_id``.  Same fail-closed / one-shot / expiry rules as
    :func:`verify_write_gate`.
    """
    if not inflight_dir:
        return (True, None)

    if not os.path.isdir(inflight_dir):
        return (False, "inflight directory does not exist")

    command_hash = hashlib.sha256(command_text.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)

    try:
        entries = os.listdir(inflight_dir)
    except OSError:
        return (False, "cannot read inflight directory")

    for name in entries:
        if not name.endswith(".gate.json"):
            continue

        gate_key = (inflight_dir, name)
        if gate_key in _consumed:
            continue

        path = os.path.join(inflight_dir, name)
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                gate = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue

        if gate.get("submit_command_sha256") != command_hash:
            continue

        if gate.get("terminal_id") != terminal_id:
            continue

        expires_text = gate.get("expires_utc")
        if expires_text is not None:
            expires = _parse_utc(expires_text)
            if expires is None:
                continue
            if now >= expires:
                continue

        _consumed.add(gate_key)
        return (True, gate.get("correlation_id"))

    return (False, "no admitted gate matches this submit")
