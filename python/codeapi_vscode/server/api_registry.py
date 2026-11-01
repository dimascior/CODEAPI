"""Registers the route groups onto a Router.

Phase 11 wired the pure-Python routes (session/health, dynamic endpoints, mesh).
Phase 12 adds the adapter-backed routes (/exec, /command, workspace/editor,
showMessage, /apis). Phase 13D adds the public /term/* routes. All adapter-backed
groups delegate to the VsCodeAdapter and return 503 when no adapter is available
(never 404). Only unknown routes 404.
"""

from . import (dynamic_endpoint_routes, mesh_routes, operations_routes,
               session_routes, term_routes, vscode_routes)


def register_all(router, *, session_manager, dynamic_registry, adapter):
    """Attach the pure (Phase 11) + adapter-backed (Phase 12) + terminal (Phase 13D)
    route groups, plus the additive machine-readable operation digest (/operations).
    The digest is pure Python and independent of /apis."""
    session_routes.register(router, session_manager)
    dynamic_endpoint_routes.register(router, dynamic_registry)
    mesh_routes.register(router)
    vscode_routes.register(router, adapter)
    term_routes.register(router, adapter)
    operations_routes.register(router)
