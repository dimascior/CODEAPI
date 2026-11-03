"""Registers the route groups onto a Router.

Phase 11 wired the pure-Python routes (session/health, dynamic endpoints, mesh).
Phase 12 adds the adapter-backed routes (/exec, /command, workspace/editor,
showMessage, /apis). Phase 13D adds the public /term/* routes. All adapter-backed
groups delegate to the VsCodeAdapter and return 503 when no adapter is available
(never 404). Only unknown routes 404.
"""

from . import (dynamic_endpoint_routes, mesh_routes, operations_routes,
               session_routes, term_routes, vscode_routes)


def register_all(router, *, session_manager, dynamic_registry, adapter,
                 restricted=True, gate_root=""):
    """Attach the pure (Phase 11) + adapter-backed (Phase 12) + terminal (Phase 13D)
    route groups, plus the additive machine-readable operation digest (/operations).
    The digest is pure Python and independent of /apis.

    *restricted* (default True) disables /exec, /command, and shell/args on
    /term/create.  *gate_root* (optional) enables write-gate verification
    against ``<gate_root>/inflight/*.gate.json``.  Both are controlled by
    environment variables in runtime.py.
    """
    session_routes.register(router, session_manager)
    dynamic_endpoint_routes.register(router, dynamic_registry)
    mesh_routes.register(router)
    vscode_routes.register(router, adapter, restricted=restricted)
    term_routes.register(router, adapter, restricted=restricted,
                         gate_root=gate_root)
    operations_routes.register(router)
