"""codeapi_vscode.runtime — the Python runtime entrypoint.

Launched by the JS adapter (extension-adapter/extension.js) as a child process,
OR run directly in dev mode:

    set PYTHONPATH=python
    python -m codeapi_vscode.runtime --host 127.0.0.1 --port 47337 --adapter-port <N>

Owns the CODEAPI HTTP service. Binds ONLY in the reserved adapter band (47337+),
never in the VS Code window band (3637-3646) — permanent policy enforced by
_guard_port + PortAllocator so legacy and adapter runtimes stay distinguishable
during discovery.

Serves three route families, all shaped byte-compatible with docs/WIRE_CONTRACT.md:
  - pure routes (health, session, dynamic endpoints, mesh) — always answered here;
  - adapter-backed routes (/exec, /command, workspace/editor, showMessage, /apis) —
    served, but return 503 {"error":"vscode adapter not available"} in
    standalone/no-adapter mode;
  - terminal routes (/term/*) — likewise served, returning the same 503 when no
    adapter is available.
Unknown unrelated routes still return 404.

Python NEVER imports or calls `vscode`.
"""

import argparse
import os
import sys

# The VS Code window band. The Python service never binds here (nor answers
# /session here) — it would poison client discovery with phantom windows.
WINDOW_BAND = range(3637, 3647)
DEFAULT_PORT_BASE = 47337  # reserved adapter band 47337-47400
# Distinct exit code for "the requested port could not be bound" — the launching
# JS adapter keys on this to retry its next port candidate (vs. 2 = config error).
ALLOCATION_EXIT_CODE = 3


class RuntimeConfigError(Exception):
    pass


def _guard_port(port: int) -> int:
    """Refuse to bind inside the VS Code window band (permanent policy)."""
    if port in WINDOW_BAND:
        raise RuntimeConfigError(
            f"refusing to bind port {port}: inside the VS Code window band "
            f"{WINDOW_BAND.start}-{WINDOW_BAND.stop - 1}, which is reserved for "
            f"legacy runtimes. The Python service binds the adapter band "
            f"({DEFAULT_PORT_BASE}+). See docs/DISCOVERY_AND_TERMINAL_ROUTING.md.")
    return port


def parse_config(argv=None):
    """Resolve host/port/adapter-port from CLI args, falling back to env."""
    p = argparse.ArgumentParser(prog="codeapi_vscode.runtime",
                                description="CODEAPI Tier-2 Python runtime")
    # No default port: an EXPLICIT port (flag or the adapter's CODEAPI_PY_PORT)
    # means "bind exactly this or fail"; no port at all means "scan the band".
    env_port = os.environ.get("CODEAPI_PY_PORT")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int,
                   default=int(env_port) if env_port else None)
    p.add_argument("--adapter-port", type=int,
                   default=int(os.environ.get("CODEAPI_EXTENSION_PORT", 0)) or None)
    args = p.parse_args(argv)
    args.workspace_uri = os.environ.get("CODEAPI_WORKSPACE_URI", "")
    args.extension_path = os.environ.get("CODEAPI_EXTENSION_PATH", "")
    if args.port is not None:
        _guard_port(args.port)
    if args.host != "127.0.0.1":
        raise RuntimeConfigError("runtime binds loopback only (127.0.0.1)")
    return args


def create_server(host, port, adapter=None):
    """Build the service bound to (host, port). Returns (HttpServer,
    SessionManager). Does NOT start serving — the caller does.

    `adapter` is the VsCodeAdapter used by the adapter-backed routes. When
    None it is built from the environment (CODEAPI_EXTENSION_PORT /
    CODEAPI_BRIDGE_TOKEN); an unconfigured adapter makes those routes answer 503
    (the pure routes are unaffected)."""
    from .server.api_registry import register_all
    from .server.dynamic_endpoint_routes import DynamicRegistry
    from .server.http_server import HttpServer
    from .server.router import Router
    from .session.session_manager import SessionManager
    from .session.window_identity import WindowIdentity
    from .vscode.adapter import VsCodeAdapter

    _guard_port(port)
    identity = WindowIdentity.from_env(port)
    session_manager = SessionManager(identity)
    dynamic_registry = DynamicRegistry()
    if adapter is None:
        adapter = VsCodeAdapter()

    router = Router()
    register_all(router, session_manager=session_manager,
                 dynamic_registry=dynamic_registry, adapter=adapter)
    return HttpServer(host=host, port=port, router=router), session_manager


def main(argv=None):
    try:
        cfg = parse_config(argv)
    except RuntimeConfigError as e:
        print(f"runtime config error: {e}", file=sys.stderr)
        return 2

    from .session.port_allocator import PortAllocationError, PortAllocator
    from .vscode.adapter import VsCodeAdapter
    # An explicit port (adapter-launched or --port) is EXACT: bind it or exit 3
    # (ALLOCATION_EXIT_CODE). The launching adapter keys on that code to retry its
    # next candidate; falling forward here would desync its probe/poll handshake.
    try:
        port = PortAllocator().resolve(cfg.port, exact=cfg.port is not None)
    except PortAllocationError as e:
        print(f"port allocation error: {e}", file=sys.stderr)
        return ALLOCATION_EXIT_CODE

    # The adapter port/token come from the launching JS adapter (CLI --adapter-port
    # / CODEAPI_EXTENSION_PORT, and CODEAPI_BRIDGE_TOKEN). If absent, adapter-backed
    # routes answer 503 while the pure routes still serve (standalone mode).
    adapter = VsCodeAdapter(port=cfg.adapter_port,
                            token=os.environ.get("CODEAPI_BRIDGE_TOKEN"))
    server, session_manager = create_server(cfg.host, port, adapter=adapter)
    print(f"codeapi runtime listening on http://{cfg.host}:{port} "
          f"(session {session_manager.id})", flush=True)
    try:
        server.start()
    except OSError as e:
        # The allocator's probe closed its socket before the real bind — a racer
        # can slip in between. Same contract as above: distinct code, adapter retries.
        print(f"port allocation error: port {port} was taken between allocation "
              f"and bind: {e}", file=sys.stderr)
        return ALLOCATION_EXIT_CODE
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
