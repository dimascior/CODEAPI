# `python/` — CODEAPI Python runtime

This directory is the CODEAPI **Python runtime service** — the running backend that the
VS Code JS adapter (`extension-adapter/extension.js`) launches and supervises. It is
built into `bin/codeapi-runtime/codeapi-runtime.exe` (PyInstaller) and shipped in the VSIX.

- **Entry:** `codeapi_runtime_main.py` → `codeapi_vscode.runtime.main()`.
- The service binds **outside the legacy window band `3637–3646`**, in the reserved band
  starting at **`47337`** (discovery scan window `47337–47400`).
- Full architecture, endpoint→logic map, and a worked example:
  [../docs/FILE_FLOW.md](../docs/FILE_FLOW.md),
  [../docs/CODEAPI_RUNTIME_ARCHITECTURE.md](../docs/CODEAPI_RUNTIME_ARCHITECTURE.md),
  [../docs/OPERATIONAL_GUIDE.md](../docs/OPERATIONAL_GUIDE.md).

## Key files

```text
python/
  codeapi_runtime_main.py    PyInstaller entry (absolute-import wrapper)
  codeapi_vscode/
    runtime.py               create_server + PortAllocator (binds 47337+) + main()
    server/                  public HTTP routes: session/health, dynamic, mesh,
                             adapter-backed /vscode/* public routes, /term/*
    session/                 session manager + port allocator + window identity
    vscode/adapter.py        VsCodeAdapter — HTTP client to the JS /vscode/* bridge
    discovery.py             window/session discovery + selection + TerminalRouter
    util/                    shared helpers
  tools/
    build_runtime.py         PyInstaller build (npm run vscode:prepublish)
    discover_and_route.py    standalone discovery/routing CLI
  tests/                     run directly: python python/tests/test_*.py
```

## Run & test

```bash
# Build the packaged runtime (also invoked by npm run vscode:prepublish):
python python/tools/build_runtime.py --run

# Run the runtime standalone (pure routes answer; adapter-backed + /term/* return 503
# because there is no JS adapter bridge):
set PYTHONPATH=python
python -m codeapi_vscode.runtime --host 127.0.0.1 --port 47355

# Tests (run each directly, not pytest):
set PYTHONPATH=python
python python/tests/test_discovery.py
python python/tests/test_runtime_pure.py
# ...and the other python/tests/test_*.py
```
