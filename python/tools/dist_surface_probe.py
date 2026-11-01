#!/usr/bin/env python3
"""Capture the live dist-runtime API surface for docs/DIST_API_INVENTORY.md.

Phase 10 evidence gate: proves what the extension actually exposes, so the
Python coverage matrix is checked against reality — not against the seed's
assumptions. The static scan (routes, registerCommand) comes from grepping
dist/extension.js; this script captures what only the running extension knows:
the live /apis list and Object.keys(codeApiTerm).

Usage:
    python python/tools/dist_surface_probe.py --port 3639
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.client import CodeAPIClient


GET_ROUTES = [
    ("health", "/health"),
    ("session", "/session"),
    ("workspace_folders", "/workspace/folders"),
    ("editor_active", "/editor/active"),
    ("mesh_peers", "/mesh/peers"),
    ("apis", "/apis"),
]

EXEC_PROBES = [
    ("workspace_name", "return vscode.workspace.name"),
    ("typeof_codeApiTerm", "return typeof codeApiTerm"),
    ("codeApiTerm_keys", "return Object.keys(codeApiTerm)"),
    ("codeApiTerm_methods",
     "return Object.keys(codeApiTerm).map(k=>({name:k,"
     "type:typeof codeApiTerm[k],"
     "arity:typeof codeApiTerm[k]==='function'?codeApiTerm[k].length:null}))"),
    ("globals_present",
     "return ['vscode','Buffer','nodePty','os','codeApiTerm']"
     ".map(n=>({name:n,typeof:(function(){try{return typeof eval(n)}"
     "catch(e){return 'undefined'}})()}))"),
]


def main():
    port = None
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    client = CodeAPIClient()
    out = {"probed_port": port}

    for name, path in GET_ROUTES:
        try:
            out[name] = client.get(path, port=port)
        except Exception as e:  # noqa: BLE001 - record failures as evidence
            out[name] = {"error": str(e)}

    for name, code in EXEC_PROBES:
        try:
            out[name] = client.exec_js(code, port=port)
        except Exception as e:  # noqa: BLE001
            out[name] = {"error": str(e)}

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
