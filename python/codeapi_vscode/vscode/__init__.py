"""vscode/ — the Python-side bridge to the minimal JS adapter.

This subpackage is the ONLY place the Python service talks to the extension host.

adapter.py  VsCodeAdapter — loopback-HTTP client to the JS adapter bridge; sends
            the per-launch X-CODEAPI-BRIDGE-TOKEN on every call and maps the
            {ok,value?} / {error} envelope to Python values / AdapterError /
            AdapterUnavailable.

The adapter-backed public routes (server/vscode_routes.py, server/term_routes.py)
delegate their leaf vscode.* / node-pty operations here, because those require the
`vscode` API (or JS eval) in-process on the extension host.
"""
