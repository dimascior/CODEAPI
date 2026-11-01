"""Tier-2 Python HTTP service for CODEAPI.

The public HTTP surface, split by concern. ``api_registry.register_all`` is the
SOLE wiring authority; it attaches exactly these route groups:

  Pure-Python routes (no vscode API):
    session_routes           /health, /session
    dynamic_endpoint_routes  /add-endpoint, dynamic GET, /remove-endpoint
    mesh_routes              /mesh/peers, /mesh/broadcast/*

  Adapter-backed routes (delegate to the JS adapter via vscode/adapter.py):
    vscode_routes            /exec, /command/:id, /workspace/folders,
                             /editor/active, /window/showMessage, /apis
    term_routes              /term/*  (typed terminal surface)

router.py + http_server.py provide routing and JSON serialization. The service
owns routing + the session registry; it never runs vscode.* itself — that is
delegated to vscode/adapter.py -> the extension-host JS adapter.
"""
