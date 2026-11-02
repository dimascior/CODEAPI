# CODEAPI

CODEAPI is a local VS Code control bridge. A minimal JavaScript **adapter** runs inside
the VS Code extension host, launches a **packaged Python runtime**, and exposes a small,
stable HTTP API on loopback for automating VS Code and driving real PTY terminals.

Primitives exposed over HTTP:

- run JavaScript inside the extension host (`/exec`) and VS Code commands (`/command/:id`)
- inspect sessions, workspaces, and the active editor (`/session`, `/workspace/folders`, `/editor/active`)
- create **visible, adapter-owned** PTY terminals and drive them (`/term/*`): write, read the
  screen buffer, observe liveness, and reap — all over HTTP
- discover running CODEAPI windows and select the right one by a durable window identity

New packages built from this branch use the extension id **`dimas.codeapi`**.

---

## Architecture (current)

```
root package.json  (main = ./extension-adapter/extension.js)
  → extension-adapter/extension.js        JS adapter (VS Code extension host)
      → launches the packaged Python runtime
        → bin/codeapi-runtime/codeapi-runtime.exe   (PyInstaller onedir)
          → python/codeapi_runtime_main.py → python/codeapi_vscode/**
            → public HTTP service on 127.0.0.1:<47337+>
               /health · /session · adapter-backed /exec /command /workspace /editor /apis
               · public /term/*  → visible adapter-owned VS Code terminal
```

- Public routes are trusted-local by default. Setting `CODEAPI_PUBLIC_TOKEN` enables `X-CODEAPI-TOKEN` authentication except for `/health` and `/session` (see [SECURITY.md](docs/SECURITY.md)).
- The **JS adapter** owns the irreducible extension-host operations (`vscode.*`, `node-pty`,
  the internal token-guarded `/vscode/*` bridge).
- The **Python runtime** owns the public HTTP service and byte-exact response shaping.
- The Python runtime binds **outside** the legacy VS Code window band `3637–3646`, in the
  reserved band starting at **47337**; `47337–47400` is the discovery **scan window**.

All documentation lives in [`docs/`](docs/).

**Design & contracts**

| Doc | What it covers |
|---|---|
| [CODEAPI_RUNTIME_ARCHITECTURE.md](docs/CODEAPI_RUNTIME_ARCHITECTURE.md) | Controlling architecture: Layers 0–4, route ownership, wire rules, invariants |
| [FILE_FLOW.md](docs/FILE_FLOW.md) | Architecture layers, file entry points, full execution flow, endpoint→logic map |
| [WIRE_CONTRACT.md](docs/WIRE_CONTRACT.md) | Exact request/response bytes per route |
| [ADAPTER_BRIDGE_CONTRACT.md](docs/ADAPTER_BRIDGE_CONTRACT.md) | Python↔JS bridge envelope + token |
| [TERM_CONTRACT.md](docs/TERM_CONTRACT.md) | Terminal routes, defaults, liveness rule |

**Operations, platform & security**

| Doc | What it covers |
|---|---|
| [INSTALL_AND_BUILD.md](docs/INSTALL_AND_BUILD.md) | Clean-clone → build → package → verify; repo↔VSIX boundary |
| [OPERATIONAL_GUIDE.md](docs/OPERATIONAL_GUIDE.md) | The one worked example: discover → select → visible terminal → run → read → reap |
| [DEBUGGING.md](docs/DEBUGGING.md) | Running, debugging, ports, liveness discipline, troubleshooting |
| [DISCOVERY_AND_TERMINAL_ROUTING.md](docs/DISCOVERY_AND_TERMINAL_ROUTING.md) | Finding windows, identity precedence, routing terminals |
| [TERMINAL_ON_WINDOWS.md](docs/TERMINAL_ON_WINDOWS.md) | Entry→endpoint terminal trace + Windows 10 process/PTY/port/privilege implications |
| [SECURITY.md](docs/SECURITY.md) | Trust boundary, risky capabilities, safe operating practice |
| [RELEASE_HYGIENE.md](docs/RELEASE_HYGIENE.md) | VSIX packaging surface + the trimmed-package validation record |

---

## Repository layout

The complete tree, annotated. **Legend:** `▶` entry point · `✔` wired / live ·
`(generated · git-ignored)` built locally, not committed.

```
CODEAPI/
├── package.json                     ▶ root manifest; main → extension-adapter/extension.js;
│                                       scripts: vscode:prepublish (build runtime), vsce:package
├── README.md                          this file
├── LICENSE.md                         MIT license
├── openapi.json                       OpenAPI sketch of the HTTP surface (reference)
├── .vscodeignore                      VSIX packaging boundary (what ships in the .vsix)
├── .gitignore                         ignores node_modules/, bin/, *.vsix, __pycache__, …
│
├── .github/
│   └── chatmodes/
│       └── codeapi.chatmode.md           VS Code chat-mode preset
│
├── .vscode/                           curated dev config (only these 4 files are tracked)
│   ├── settings.json
│   ├── tasks.json                     build/test tasks (install · build runtime · package VSIX)
│   ├── launch.json                    Run-Extension + standalone-runtime debug configs
│   └── extensions.json
│
├── extension-adapter/                 LAYER 0–1 · the JS adapter (runs in the VS Code host)
│   ├── extension.js                 ▶ activate()/deactivate(); startRuntime() spawns the Python
│   │                                   runtime; the /vscode/* + /vscode/term/* bridge; commands
│   ├── bridge_core.js                 vscode-free logic: runExecBody (7 /exec globals),
│   │                                   apisFromCommands (13 fixed /apis descriptors),
│   │                                   resolveTermDefaults, makeCodeApiTerm (codeApiTerm facade)
│   ├── package.json                   the adapter's own manifest (declares the node-pty dep)
│   └── tests/
│       └── test_bridge_core.js        Node unit tests for bridge_core (no VS Code needed)
│
├── python/                            LAYER 2–3 · the Python runtime + control plane
│   ├── codeapi_runtime_main.py      ▶ PyInstaller entry: from codeapi_vscode.runtime import main
│   ├── pyproject.toml                 package metadata (stdlib-only; requires-python ≥ 3.8)
│   │
│   ├── codeapi_vscode/                the package
│   │   ├── __init__.py
│   │   ├── __main__.py              ▶ `python -m codeapi_vscode`  (CLI entry)
│   │   ├── runtime.py              ▶ create_server(), PortAllocator wiring, main()
│   │   ├── cli.py                     argparse CLI (client / control-plane surface)
│   │   ├── client.py                  CodeAPIClient — HTTP client to a runtime
│   │   ├── terminal_client.py         TermClient — client-side /term helper
│   │   ├── discovery.py             ▶ discover_sessions · classify_runtime · compute_flags ·
│   │   │                               select_by_identity · TerminalRouter   (Layer 3)
│   │   ├── run_result.py              result value type
│   │   ├── types.py                   shared dataclasses
│   │   ├── errors.py                  CodeAPIError / AdapterError / AdapterUnavailable
│   │   │
│   │   ├── server/                    the public HTTP service (Layer 2)
│   │   │   ├── api_registry.py      ▶ register_all() — THE wiring authority
│   │   │   ├── router.py              (method, path) → handler; path params; JSON 404
│   │   │   ├── http_server.py         stdlib http.server; NO_BODY sentinel; compact JSON
│   │   │   ├── session_routes.py    ✔ GET /health, GET /session
│   │   │   ├── dynamic_endpoint_routes.py  ✔ /add-endpoint · dynamic GET · /remove-endpoint
│   │   │   ├── mesh_routes.py       ✔ /mesh/peers · /mesh/broadcast/:endpoint (inert)
│   │   │   ├── vscode_routes.py     ✔ /exec · /command/:id · /workspace/folders ·
│   │   │   │                           /editor/active · /window/showMessage · /apis
│   │   │   └── term_routes.py       ✔ GET/POST/DELETE /term/*
│   │   │
│   │   ├── vscode/                    the ONLY Python → extension-host path
│   │   │   └── adapter.py           ✔ VsCodeAdapter — bridge client; sends the token every call
│   │   │
│   │   ├── session/                  identity + port binding
│   │   │   ├── session_manager.py   ✔ builds the /session payload
│   │   │   ├── window_identity.py   ✔ identity fields incl. codeapiWindowId (from spawn env)
│   │   │   └── port_allocator.py    ✔ binds 47337+, refuses 3637–3646
│   │   │
│   │   └── util/
│   │       └── json.py              ✔ now_iso() · loads() · strip_ansi()
│   │
│   ├── tests/                         self-executing scripts (NOT pytest — run each directly)
│   │   ├── README.md
│   │   ├── test_smoke.py              portless CLI smoke
│   │   ├── test_differential.py       Python wrappers ↔ raw /exec differential (needs a live window)
│   │   ├── test_runtime_pure.py       pure + adapter-503 + /term-503 routes, no VS Code
│   │   ├── test_runtime_cli.py        runtime bootstrap (port guard, health, session)
│   │   ├── test_adapter_client.py     VsCodeAdapter vs a fake bridge
│   │   ├── test_adapter_term.py       adapter terminal methods vs a fake bridge
│   │   ├── test_vscode_routes.py      adapter-backed route shaping (fake adapter)
│   │   ├── test_term_routes.py        public /term/* shaping (fake adapter)
│   │   └── test_discovery.py          discovery classifiers + selection (network-free)
│   │
│   └── tools/
│       ├── build_runtime.py         ▶ PyInstaller onedir build → bin/codeapi-runtime/
│       ├── discover_and_route.py      ops CLI: discovery table + terminal-routing demo
│       ├── dist_surface_probe.py      probe the legacy dist HTTP surface (historical)
│       ├── phase13e_terminal_differential.py   terminal-liveness harness (historical)
│       └── phase14c_devload_probe.py  read-only dev-load discovery probe (historical)
│
├── scripts/
│   └── search.ps1                     repo source search for CODEAPI terminals: prefers rg,
│                                       falls back to Select-String (prints a notice) when rg
│                                       is not on PATH; excludes .git/node_modules/bin/build/
│                                       dist/__pycache__ · `pwsh scripts/search.ps1 <pat> [path]`
│
├── docs/                              all documentation (never shipped in the VSIX)
│   ├── CODEAPI_RUNTIME_ARCHITECTURE.md   controlling design: Layers 0–4, ownership, invariants
│   ├── FILE_FLOW.md                      layers, entry points, execution flow, endpoint→logic map
│   ├── WIRE_CONTRACT.md                  exact request/response bytes per route
│   ├── ADAPTER_BRIDGE_CONTRACT.md        Python↔JS bridge envelope + token
│   ├── TERM_CONTRACT.md                  terminal routes, defaults, liveness rule
│   ├── DISCOVERY_AND_TERMINAL_ROUTING.md window discovery, identity, routing
│   ├── TERMINAL_ON_WINDOWS.md            entry→endpoint trace + Windows 10 implications
│   ├── INSTALL_AND_BUILD.md              clone → build → package → verify; repo↔VSIX boundary
│   ├── OPERATIONAL_GUIDE.md              the one worked end-to-end example
│   ├── DEBUGGING.md                      running, ports, liveness, troubleshooting
│   ├── SECURITY.md                       trust boundary, risky capabilities, safe practice
│   └── RELEASE_HYGIENE.md                VSIX packaging surface + validation record
│
├── bin/                    (generated · git-ignored)  PyInstaller onedir — SHIPS in the VSIX
│   └── codeapi-runtime/
│       ├── codeapi-runtime.exe        the packaged Python runtime (Windows build)
│       └── _internal/                 python38.dll · base_library.zip · *.pyd · CRT DLLs · …
│
└── node_modules/           (generated · git-ignored)  only node_modules/node-pty SHIPS in the VSIX
```

Two things this tree makes explicit:

- **`server/api_registry.py` is the wiring authority.** It registers the five `✔` route
  modules plus `vscode/adapter.py` — the whole served surface. (The Phase-9
  `NotImplementedError` skeletons that once shadowed these route names were removed; see
  [FILE_FLOW.md § Wired modules](docs/FILE_FLOW.md).)
- **The VSIX ships only** `package.json`, `readme.md`, `LICENSE.md`, `extension-adapter/**`,
  `bin/codeapi-runtime/**`, and `node_modules/node-pty/**`; `.vscodeignore` excludes
  everything else. `bin/codeapi-runtime/` here is a **Windows** build — on macOS/Linux
  build per-platform and run dev mode (see
  [Platform support](#platform-support-windows--macos--linux)).

---

## Quick start

```bash
git clone git@github.com:dimascior/CODEAPI.git CODEAPI && cd CODEAPI
npm install                 # node-pty + @vscode/vsce
npm run vscode:prepublish   # build bin/codeapi-runtime/codeapi-runtime.exe (PyInstaller)
npm run vsce:package        # -> codeapi-<version>.vsix
```

Then install the VSIX (or press **F5 → Run Extension**), and verify:

```bash
curl -s http://127.0.0.1:47337/health     # {"status":"ok"}
curl -s http://127.0.0.1:47337/session
```

- Install / build / package details, prereqs, and the repo↔VSIX boundary:
  [INSTALL_AND_BUILD.md](docs/INSTALL_AND_BUILD.md)
- The full end-to-end example (discover → select → visible terminal → run → read → reap):
  [OPERATIONAL_GUIDE.md](docs/OPERATIONAL_GUIDE.md)
- Running, debugging, ports, liveness, and troubleshooting: [DEBUGGING.md](docs/DEBUGGING.md)
- **macOS / Linux:** the packaged-`.exe` path above is Windows-only today — use dev
  mode instead; see [Platform support](#platform-support-windows--macos--linux).

VS Code developer config lives in `.vscode/` (tracked): build/test **Tasks** and
Run-Extension / standalone-runtime **launch** configs.

---

## How an agent executes a terminal command

This is the **authoritative, layer-by-layer sequence** for an automation agent that
wants to run a shell command inside a specific VS Code window and read the result. It
supersedes the older CLI-first guidance in [`AGENTS.md`](AGENTS.md), which still
targets the **retired** `codeapi`/`codeApiTerm` CLI on the legacy `3637–3646` band. Drive the
**public `/term/*` routes** on a `47337+` runtime instead.

There are two equivalent transports — raw HTTP (`curl`, any language) or the bundled
Python control plane (`codeapi_vscode.discovery`). Both hit the same routes and cross
the same layers. Layer/endpoint references throughout:
[FILE_FLOW.md](docs/FILE_FLOW.md) (endpoint→logic map),
[TERM_CONTRACT.md](docs/TERM_CONTRACT.md) (shapes + liveness),
[DISCOVERY_AND_TERMINAL_ROUTING.md](docs/DISCOVERY_AND_TERMINAL_ROUTING.md) (selection),
[TERMINAL_ON_WINDOWS.md](docs/TERMINAL_ON_WINDOWS.md) (what the OS actually does).

### Step 0 — Precondition: a runtime must be live

The extension must be active (installed VSIX, or **F5 → Run Extension**). On activation
the adapter (`extension-adapter/extension.js` → `activate()` → `startRuntime()`) spawns
the Python runtime, which binds `127.0.0.1:<47337+>`. Confirm with `GET /health` →
`{"status":"ok"}`. **Health is not identity** — a stale or duplicate runtime also
returns `200`, so never stop here.

### Step 1 — Discover every CODEAPI runtime

Scan both bands (`3637–3646` legacy + `47337–47400` adapter). For each responder read
`/health` + `/session` and probe `GET /term/list`. Classification (`classify_runtime`
in `discovery.py`): `/term/*` present → **adapter** (a Python runtime that can own
terminals); `/term/*` `404` on the legacy band → **legacy bundle** (cannot). Only an
**adapter** runtime with `terminal_routes == "live"` is routable.
*Layers touched:* Layer 3 client (`discovery.py`) → Layer 2 pure route `/session`
(`server/session_routes.py`) + terminal probe (`server/term_routes.py`).

### Step 2 — Select the target window by durable identity

`/session` carries several identity signals; prefer the most durable present
(precedence): **`codeapiWindowId`** > `vscodeSessionId` > `extensionHostPid` >
`workspaceUri` > `serverPort`. `workspaceUri` is **ambiguous** — two windows on one
folder share it. Use `select_by_identity(sessions, "<codeapiWindowId>",
require_terminals=True)`. Picking "whichever port answers first" targets the wrong
window when several are alive (the normal post-cutover state).

### Step 3 — Create a visible terminal

`POST /term/create {"shell":"…","show":true}`.
*Layer path:* Layer 2 `term_routes.create_handler` → `VsCodeAdapter.term_create`
(`vscode/adapter.py`, attaches `X-CODEAPI-BRIDGE-TOKEN`) → Layer 1 bridge
`handleTermCreate` → **`node-pty.spawn` inside the extension host** → a real shell
process (+ a visible `codeapi:<id>` terminal when `show:true`). Returns `{id,shell,cwd,pid}`.

- **Choose the shell explicitly.** The default is `COMSPEC` → **cmd.exe** on Windows,
  `$SHELL || /bin/bash` on macOS/Linux. Pass `"shell":"powershell.exe"` (Windows) or
  `"shell":"/bin/bash"` (posix) so quoting is predictable.
- `show:true` makes the terminal physically appear in the window — visual confirmation
  you targeted correctly.

### Step 4 — Send the command

`POST /term/:id/write {"data":"<cmd>","enter":true}` → … → `pty.write(data + "\r")`.
Returns `{id,"wrote":N}` = **bytes accepted into the PTY input — NOT command success,
NOT proof of life.** Shell metacharacters (`& | ^ < >`) pass through every layer
untouched and are interpreted **only by the target shell** (JSON carries data, not
code). For newline-heavy or shell-hostile payloads, base64-encode and decode in the
shell rather than fighting the line editor.

### Step 5 — Read the output

`GET /term/:id/read[?tail=N&clear=bool]` → `{id,bytes,buffer}`. The buffer is the **raw
ANSI/VT screen** (ConPTY on Windows; a native PTY on posix) — not clean stdout. Parse
the region *after* your command's echo and strip ANSI. **The buffer is the success
signal, not `wrote`.** Do not pass `clear=true` until you have captured what you need
(it also erases the exit marker).

### Step 6 — Observe exit (liveness)

Poll `GET /term/list` until the record shows **`alive:false` AND an integer
`exitCode`.** Liveness comes from node-pty's `onExit` (authoritative); the
`process.kill(pid,0)` fallback can flip `alive:false` a beat before `exitCode` is set,
so wait for **both**. **Never infer exit from `DELETE`.**

### Step 7 — Reap

`DELETE /term/:id` → `pty.kill()` + dispose + remove from registry → `{id,killed}`.
`killed:false` *after* an observed exit is **expected** (the process was already gone).
A follow-up `GET /term/:id/read` → `404` confirms cleanup. Always reap terminals you
create — their buffers may hold secrets.

### Full layer map of a single write

```
agent  (curl / discovery.TerminalRouter)        Layer 3 — client / control plane
  → POST /term/:id/write
  → term_routes.write_handler                    Layer 2 — public route (Python)
  → VsCodeAdapter.term_write  (+ bridge token)   Layer 2 → 1 — bridge client
  → handleTermWrite  (token already verified)     Layer 1 — JS adapter bridge
  → pty.write(data + "\r")                        Layer 0 — extension host (node-pty)
  → ConPTY / PTY → shell parses the keystrokes    OS endpoint — a real Windows/posix process
```

### Rules an agent must not break

- **Health ≠ identity** — select by `codeapiWindowId`.
- **`wrote` ≠ success** — read the buffer to confirm the command ran.
- **Liveness from `/term/list`** (`alive:false` + integer `exitCode`), never from `DELETE`.
- **Choose the shell explicitly** — the default differs by OS (cmd vs `/bin/bash`).
- **Reap PTYs**, and treat captured buffers as sensitive.
- **`/vscode/*` are internal** (403 without the token) — always call the **public** routes.

### Minimal driver (Python control plane)

```python
# PYTHONPATH=python   (interpreter: python on Windows, python3 on macOS/Linux)
from codeapi_vscode.discovery import discover_sessions, select_by_identity, TerminalRouter

sessions = discover_sessions()                        # scans 3637-3646 + 47337-47400
target   = select_by_identity(sessions, "<codeapiWindowId>", require_terminals=True)
print(target.codeapi_window_id, target.vscode_session_id, target.extension_host_pid,
      target.server_port, target.workspace_uri)       # confirm you resolved the right window
result   = TerminalRouter(target.base_url).run_and_reap(shell="powershell.exe")
print(result["ok"], result["exit_code"])              # create→write→read→exit→reap, proven
```

The equivalent raw-HTTP recipe (Windows and posix) is in
[OPERATIONAL_GUIDE.md](docs/OPERATIONAL_GUIDE.md) and
[TERMINAL_ON_WINDOWS.md § 10](docs/TERMINAL_ON_WINDOWS.md).

---

## Platform support (Windows / macOS / Linux)

The full CODEAPI flow has been validated end-to-end **only on Windows 10
(10.0.19045)**. macOS and Linux remain **unvalidated** targets.

| Component | Windows | macOS/Linux |
|---|---|---|
| Packaged runtime | `codeapi-runtime.exe` (validated) | `codeapi-runtime` (launcher corrected, unvalidated) |
| Development fallback | real `python.exe` (validated) | `python3` (launcher corrected, unvalidated) |
| Terminal backend | node-pty ConPTY (validated) | node-pty native PTY (unvalidated) |
| Host-native VSIX | validated on Windows 10 | unvalidated |

`python/tools/build_runtime.py` now prints the correct host-native
executable name. PyInstaller cannot cross-compile: run `npm install` and
`npm run vscode:prepublish` on each target platform and package a matching
native node-pty binary. macOS/Linux development mode uses `python3` from
PATH (or an explicit `codeapi.pythonPath` / `CODEAPI_PYTHON`).

Before claiming full support, validate extension activation, identity
discovery, `/exec`, PTY create/write/read/liveness/delete, packaged file
inventory and installed-VSIX native binary loading on each host.
See [INSTALL_AND_BUILD.md](docs/INSTALL_AND_BUILD.md).

---

## Repo vs VSIX boundary

The repo carries source, tests, docs, and dev tooling. The VSIX ships **only** the
runtime surface: `package.json`, `readme.md`, `LICENSE.md`, `extension-adapter/**`,
`bin/codeapi-runtime/**`, `node_modules/node-pty/**`. Everything else (`python/**`,
`docs/**`, `.vscode/**`, …) is excluded by `.vscodeignore`. See
[INSTALL_AND_BUILD.md § Repo vs VSIX boundary](docs/INSTALL_AND_BUILD.md#repo-vs-vsix-boundary).

---

## Legacy / history

- **Phase 15 retired the old surfaces.** The sourceless `dist/extension.js` runtime, the
  MCP server, the Node/TS CLI (`legacy-seed/`), the socket `extension-bridge/`, the
  TS/esbuild build scaffolding, and the old `scripts/` debug helpers were removed from the
  tree (today's `scripts/` carries only the `search.ps1` source-search helper). The dated `docs/PHASE_*` and `docs/PYTHON_PORT_*` evidence files — and the legacy
  operating docs (`API.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `DEVELOPMENT.md`,
  `DIST_API_INVENTORY.md`, `CODEX_CODEAPI_OPERATING_MODEL.md`, `CONTINUE_LOCAL_DEV.md`) — were
  also removed. The current architecture is described only by the docs in the index above.
- The untracked seed CLI that preceded the package has been deleted from disk; the
  canonical local CLI is `python -m codeapi_vscode.cli` (the `codeapi` command on PATH).
- The older extension `dimas.vscode-api-expose` (the legacy bundle on the `3637–3646`
  band) is **superseded** by `dimas.codeapi` and should be uninstalled; if both are
  installed they use different port bands and do not conflict.

---

## Safety

CODEAPI exposes arbitrary extension-host JavaScript execution and PTY shell control. It is
for local automation on a trusted machine, bound to loopback only. Do not expose the HTTP
ports to a network interface without adding authentication and an explicit policy layer.
