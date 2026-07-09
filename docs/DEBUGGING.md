# CODEAPI Debugging & Operations

How to run, inspect, and troubleshoot the running system. Companion to
[FILE_FLOW.md](FILE_FLOW.md), [INSTALL_AND_BUILD.md](INSTALL_AND_BUILD.md),
[OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md).

---

## From the VS Code command palette

The adapter contributes two commands (see `package.json` → `contributes.commands`):

- **`CODEAPI: Show Runtime Output`** (`codeapi.showRuntimeLog`) — opens the
  **"CODEAPI Runtime"** output channel. It logs the exact spawn line
  (`[starting runtime: <exe> <args> (cwd=…)]`), health status, and any spawn error.
- **`CODEAPI: Restart Python Runtime`** (`codeapi.restartRuntime`) — stops the current
  Python child and respawns it against the same bridge (it picks a fresh free port; the
  durable `codeapiWindowId` is preserved).

The output-channel log is also persisted to disk under
`<user-data-dir>\logs\<ts>\window1\exthost\output_logging_*\<n>-CODEAPI Runtime.log`,
which is readable without focusing the GUI.

---

## Searching the source (works without ripgrep)

Agent guidance often prefers `rg` (ripgrep), but a **CODEAPI-created PowerShell terminal
on a clean Windows box may not have `rg` on PATH** — the first source probe then fails.
Use the repo-local helper, which prefers `rg` and otherwise falls back to `Select-String`
(and says so):

```powershell
pwsh scripts/search.ps1 "term_routes" python
# rg if present, else:
#   [search] ripgrep (rg) not found on PATH - falling back to Select-String.
#   <path>:<line>:<match>
```

It excludes `.git`, `node_modules`, `bin`, and `__pycache__` either way. If you call
`rg` directly, confirm it exists first: `Get-Command rg`. In a Git-Bash terminal the
equivalent is `grep -rn <pattern> <path>` (or `rg` if installed).

---

## Debugging the extension in a dev host

1. Build the runtime once: **Tasks: Run Task** → `build runtime`
   (so `bin/codeapi-runtime/codeapi-runtime.exe` exists for packaged mode).
2. Press **F5** → **Run Extension (CODEAPI adapter)** (`.vscode/launch.json`). This opens
   an Extension Development Host loading this repo (`package.json.main` =
   `extension-adapter/extension.js`).
3. To debug the Python service in isolation, use **Python: CODEAPI runtime (standalone)** —
   it runs `python -m codeapi_vscode.runtime` (pure routes answer; adapter-backed and
   `/term/*` return 503 without a bridge).

**Runtime resolution order** (in `startRuntime`): `codeapi.pythonPath` setting →
`CODEAPI_PYTHON` env → `python.exe` on `PATH`. **Packaged mode wins first**: if
`<extensionPath>/bin/codeapi-runtime/codeapi-runtime.exe` exists it is used and no Python
is needed; otherwise it falls back to `python -m codeapi_vscode.runtime` (dev mode).

---

## Ports: legacy band vs the Python runtime band

| Band | What answers there | Notes |
|---|---|---|
| **3637–3646** | the **legacy** sourceless bundle (`dimas.vscode-api-expose`), if installed | the old window band; has **no** `/term/*` (404) |
| **47337–47400** | the **new** Python runtime (this architecture) | the **discovery scan window** |

`47337–47400` is the **range discovery scans**, *not* a fixed bind range. The runtime
binds the **next free port at/after 47337** (allocator reserved band `47337–47346`); it
refuses to bind inside `3637–3646`. Ports therefore drift as multiple windows/runtimes
come and go.

Inspect what is listening:

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object { $_.LocalPort -ge 47337 -and $_.LocalPort -le 47400 } |
  Select-Object LocalPort, OwningProcess
```

Or classify every responder with the discovery tool:

```bash
set PYTHONPATH=python
python python/tools/discover_and_route.py --no-route
```

---

## Health / identity checklist

- **`package.json.main`** should read `./extension-adapter/extension.js` (the adapter is
  the entrypoint; not `dist/extension.js`).
- **`GET /health`** → `{"status":"ok"}` means the runtime is up. Health alone is **not**
  identity — a stale/duplicate runtime also returns 200.
- **`GET /session`** carries identity. Prefer the most durable field present:
  `codeapiWindowId` > `vscodeSessionId` > `extensionHostPid` > `workspaceUri` > `serverPort`.
  `windowId` stays `0` and is **not** used for identity.
- **Bridge token env** — the adapter mints `CODEAPI_BRIDGE_TOKEN` and passes it (plus
  `CODEAPI_PY_PORT`, `CODEAPI_EXTENSION_PORT`, `CODEAPI_WORKSPACE_URI`,
  `CODEAPI_EXTENSION_PATH`, `CODEAPI_VSCODE_SESSION_ID`, `CODEAPI_EXTENSION_HOST_PID`,
  `CODEAPI_WINDOW_ID`) to the runtime. Every `/vscode/*` bridge call must carry
  `X-CODEAPI-BRIDGE-TOKEN`; a missing/wrong token → `403 {"error":"forbidden"}`.
- **`node-pty` loads** in the real extension host — verify with
  `POST /exec` body `return typeof nodePty` → `"object"`. If node-pty fails to load,
  `/exec` and `/term/*` fail exactly as the legacy runtime would.

---

## Terminal liveness discipline

- Read the **`alive`** + **`exitCode`** fields from **`GET /term/list`**. A process has
  exited only when `alive:false` **and** `exitCode` is an integer.
- **Do not use `DELETE` as a liveness signal** — it returns `killed:false` when the
  process already exited. `DELETE` is cleanup only.
- `wrote:N` from `/term/:id/write` means *N bytes accepted*, not that the command
  succeeded. Always **read the buffer** (`GET /term/:id/read`) to see what happened.
- `reconcileAlive` (a `process.kill(pid,0)` fallback) can flip `alive:false` a beat
  before `exitCode` is set — poll until **both** are present.

---

## Common symptoms

| Symptom | Likely cause | Action |
|---|---|---|
| `/health` never comes up | runtime spawn failed | Show Runtime Output; check `python` resolvability / packaged exe presence |
| adapter-backed routes → 503 | no bridge (standalone runtime) or adapter unreachable | run inside a dev host / installed VSIX, not standalone |
| `/term/*` → 404 on 3637–3646 | that's the **legacy** bundle | target the 47337+ Python runtime instead |
| `403 forbidden` on `/vscode/*` | missing/wrong bridge token | these are internal routes; call the **public** routes instead |
| PyInstaller build `WinError 5` | a running runtime holds the repo-bin exe open | stop the runtime using that exact `bin\codeapi-runtime\codeapi-runtime.exe` path, then rebuild |
