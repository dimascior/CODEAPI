"""Data contract + shared primitives for the CODEAPI Python client.

Ported from the retired seed CLI (Phase 10). Holds the wire dataclasses,
the shared constants/regexes, and the low-level helpers (_from_dict, _term_id,
_strip_ansi, _print, _analyze_buffer) used across client.py and terminal_client.py.

Behavior is the seed's, byte-for-byte; only the module layout changed. `RunResult`
and its parsers live in run_result.py.
"""

import json
import re
import sys
from dataclasses import dataclass, field, MISSING
from typing import List, Optional

from .errors import CodeAPIError, ValidationError


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# CODEAPI runtime port bands. LEGACY = the old sourceless bundle's VS Code window
# band; ADAPTER = the reserved band the post-cutover Python runtime binds in (also
# discovery's scan window). The CLI scans BOTH so it finds either runtime; an explicit
# --port is probed directly even outside these bands (see CodeAPIClient.target).
LEGACY_PORTS = list(range(3637, 3647))        # 3637-3646
ADAPTER_PORTS = list(range(47337, 47401))     # 47337-47400
DEFAULT_PORTS = LEGACY_PORTS + ADAPTER_PORTS
DISCOVERY_TIMEOUT = 1.0
TERM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PS_PROMPT_RE = re.compile(r"PS\s+([A-Za-z]:\\[^>]*)>\s*$")
CMD_PROMPT_RE = re.compile(r"^([A-Za-z]:\\[^>]*)>\s*$")


# ---------------------------------------------------------------------------
# Dataclasses — the data contract between Python and the extension
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    """GET /session response shape."""
    id: str
    pid: int
    serverPort: int
    workspaceUri: Optional[str] = None
    windowId: int = 0
    capabilities: List[str] = field(default_factory=list)
    lastSeen: str = ""
    meshPeers: List[str] = field(default_factory=list)
    # Adapter-pinned identity (additive; present on post-cutover runtimes, None on
    # legacy). See docs/DISCOVERY_AND_TERMINAL_ROUTING.md.
    vscodeSessionId: Optional[str] = None
    extensionHostPid: Optional[int] = None
    codeapiWindowId: Optional[str] = None


@dataclass
class SessionRecord:
    """One discovered VS Code window."""
    port: int
    info: SessionInfo
    base_url: str


@dataclass
class TermInfo:
    """One entry from codeApiTerm.list()."""
    id: str
    pid: int
    bytes: int


@dataclass
class TermCreateResult:
    """codeApiTerm.create() return value."""
    id: str
    shell: str
    cwd: str
    pid: int


@dataclass
class TermWriteResult:
    """codeApiTerm.write() return value."""
    id: str
    wrote: int


@dataclass
class TermReadResult:
    """codeApiTerm.read() return value."""
    id: str
    bytes: int
    buffer: str


@dataclass
class TermResizeResult:
    """codeApiTerm.resize() return value."""
    id: str
    cols: int
    rows: int


@dataclass
class TermKillResult:
    """codeApiTerm.kill() return value."""
    id: str
    killed: bool


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _from_dict(cls, data, strict=False):
    """Build a dataclass from a dict, ignoring unknown keys, using defaults
    for missing optional fields. In strict mode, validates required fields
    and surfaces unknown keys."""
    if not isinstance(data, dict):
        if strict:
            raise ValidationError(
                f"expected dict for {cls.__name__}, got {type(data).__name__}")
        return data
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data.keys()) - known
    filtered = {k: v for k, v in data.items() if k in known}
    if strict:
        missing = []
        for f in cls.__dataclass_fields__.values():
            if (f.default is MISSING and f.default_factory is MISSING
                    and f.name not in filtered):
                missing.append(f.name)
        if missing:
            raise ValidationError(
                f"{cls.__name__} missing required field(s): "
                f"{', '.join(missing)}")
        if unknown:
            print(f"STRICT: {cls.__name__} received unknown field(s): "
                  f"{', '.join(sorted(unknown))}", file=sys.stderr)
    return cls(**filtered)


def _term_id(value):
    if not TERM_ID_RE.match(value):
        raise CodeAPIError(f"invalid terminal id: {value!r}")
    return value


def _strip_ansi(text):
    """Remove ANSI/VT escape sequences."""
    if not text:
        return text
    return ANSI_RE.sub("", text)


def _print(value, as_json=False):
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, default=str))
    elif value is None:
        print("No return value")
    else:
        print(value)


def _analyze_buffer(clean, term_id):
    """Extract shell type, cwd, prompt, and idle state from stripped buffer."""
    lines = [ln.rstrip("\r") for ln in clean.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    last = lines[-1].strip() if lines else ""

    shell = "unknown"
    cwd = None
    idle = False

    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = PS_PROMPT_RE.search(stripped)
        if m:
            shell = "powershell"
            cwd = m.group(1)
            idle = bool(PS_PROMPT_RE.search(last))
            break
        m = CMD_PROMPT_RE.match(stripped)
        if m:
            shell = "cmd"
            cwd = m.group(1)
            idle = bool(CMD_PROMPT_RE.match(last))
            break

    return {"id": term_id, "shell": shell, "cwd": cwd,
            "idle": idle, "prompt": last}
