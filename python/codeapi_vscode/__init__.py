"""codeapi_vscode — Python service + control plane for CODEAPI.

Forward architecture replacing the Node CLI (``legacy-seed/cli/src/cli.ts``)
and the MCP server (``legacy-seed/legacy-seed/mcp-server``). MCP is NOT the
architecture; see ``docs/PYTHON_PORT_COMPATIBILITY.md``.

Status by tier:
  1. control plane / client  — PORTED (Phase 10): client.py, terminal_client.py,
                               types.py, run_result.py, errors.py, cli.py. Real
                               code, differential-equivalent to the seed
                               ``the retired seed CLI``.
  2. Python service          — SKELETON (Phase 11+): server/, session/, terminal/
  3. minimal JS adapter      — NOT here; it is the extension-host extension.js,
                               reached from vscode/adapter.py (Phase 12).

See ``docs/PYTHON_PORT_ROADMAP.md``.
"""

__version__ = "0.0.0"
__phase__ = 10
__status__ = "tier1-ported"
