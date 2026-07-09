# CODEAPI File Flow — Layers, Entry Points, Execution Paths

This is the **current operational truth** for how CODEAPI is wired after Phase 15.
It maps the running system layer-by-layer, names the file that starts each part,
traces a request end-to-end, and maps every public endpoint to the code that
answers it.

Companion docs: [CODEAPI_RUNTIME_ARCHITECTURE.md](CODEAPI_RUNTIME_ARCHITECTURE.md)
(controlling design + wire rules), [OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md)
(full worked example), [INSTALL_AND_BUILD.md](INSTALL_AND_BUILD.md),
[DEBUGGING.md](DEBUGGING.md).

---

## 1. Architecture layers

The layer numbers below match the controlling
[CODEAPI_RUNTIME_ARCHITECTURE.md](CODEAPI_RUNTIME_ARCHITECTURE.md) (Layers 0–4).
Read the diagram top-down as the **request path**. The single most important fact
it encodes: the Python runtime is a **separate child process**, not code running
inside the VS Code extension host. The real process boundary is between Layer 1
and Layer 2 — that is where the spawn happens and the env-var contract crosses.
(Lifecycle runs the other way: at activation the Layer-1 adapter spawns and
supervises the Layer-2 child — see §2.)

```
┌─ Layer 3 · client / control plane ─────────────────────────────────────────
│  curl · python -m codeapi_vscode · discovery.py · TerminalRouter · any agent
│  Scans responders, selects the target window, drives the public HTTP API.
└─────────────────────────────────────────────────────────────────────────────
            │  public HTTP  →  127.0.0.1:<47337+>
            │  /health  /session  /exec  /command/:id  /term/*  /apis  …
            ▼
┌─ Layer 2 · Python runtime service  (SEPARATE child process) ───────────────
│  bin/codeapi-runtime/codeapi-runtime.exe   (PyInstaller onedir)
│  entry python/codeapi_runtime_main.py → codeapi_vscode.runtime:main
│    router + http_server .......... server/router.py · server/http_server.py
│    pure routes ................... session_routes · mesh_routes · dynamic_…
│    adapter-backed routes ......... server/vscode_routes.py       (→ bridge)
│    terminal routes ............... server/term_routes.py         (→ bridge)
│    discovery + session identity .. discovery.py · session/*
│    bridge client (token on every)  vscode/adapter.py
│  Owns the PUBLIC wire contract; shapes every byte; maps errors 503/500/404.
│  Never imports `vscode`; reaches it ONLY by calling Layer 1 ↓
└─────────────────────────────────────────────────────────────────────────────
            │  POST /vscode/*  (loopback, ephemeral bridge port)
            │  + X-CODEAPI-BRIDGE-TOKEN        (missing/wrong → 403 forbidden)
            ▼
┌─ Layers 0–1 · VS Code extension host + JS adapter bridge  (ONE Node process) ─
│  Layer 0  the Electron host: provides `vscode` + node-pty; owns visible
│           terminals and the Python child-process lifecycle.
│  Layer 1  extension-adapter/extension.js  activate()/deactivate();
│             startRuntime() spawns + supervises the Layer-2 child and serves
│             the internal /vscode/* bridge on an ephemeral loopback port.
│           extension-adapter/bridge_core.js  vscode-free logic: the 7 /exec
│             globals, /apis fixed descriptors, term defaults, codeApiTerm facade.
│  ══ the ONLY place vscode.* and node-pty actually execute ══
└─────────────────────────────────────────────────────────────────────────────

Packaging planes — orthogonal to the request path, NOT runtime layers:
  • VSIX boundary   .vscodeignore  → ships adapter + runtime exe + node-pty only
  • Repo-only       docs/ · .vscode/ · python/tests/ · python/tools/ — never shipped
```

- **Layer 0 — VS Code extension host (Node/Electron).** Provides the `vscode` API
  object and `node-pty`; owns `activate`/`deactivate`, visible terminals, and the
  Python child-process lifecycle. Same OS process as Layer 1.
- **Layer 1 — JS adapter + internal bridge (`extension-adapter/`).** A thin,
  hand-written shim (`extension.js`) plus a vscode-free helper (`bridge_core.js`).
  It launches and supervises the Python runtime and serves the loopback `/vscode/*`
  bridge — the single place `vscode.*` / `node-pty` run. One envelope
  `{ok:true,value?}` / `{error}`; every call requires `X-CODEAPI-BRIDGE-TOKEN`.
  **Layers 0 and 1 are one process**; the load-bearing boundary is Layer 1 ↔ Layer 2.
- **Layer 2 — Python runtime service (`bin/codeapi-runtime/…exe`, source
  `python/codeapi_vscode/`).** A separate child process. Owns the public HTTP
  surface and the byte-exact wire contract: pure routes answered locally
  (`/health`, `/session`, mesh, dynamic), adapter-backed routes forwarded to Layer 1
  (`/exec`, `/command/:id`, `/workspace/folders`, `/editor/active`,
  `/window/showMessage`, `/apis`), and `/term/*`. Maps adapter errors to 503/500/404.
  **Never imports `vscode`.** In dev it can also run as `python -m codeapi_vscode.runtime`.
- **Layer 3 — client / control plane.** Whatever drives the public API from outside:
  `curl`, `python -m codeapi_vscode`, and the discovery layer (`discovery.py`,
  `TerminalRouter`, `python python/tools/discover_and_route.py`) that scans,
  classifies, and selects the right window before routing a terminal to it.
- **Layer 4 — compatibility / cutover (historical).** `package.json.main` now points
  at the adapter (`./extension-adapter/extension.js`); the legacy sourceless
  `dist/extension.js` is retired. Retained as a named layer only for traceability.

**Packaging planes** (orthogonal — not steps in the request path):

- **VSIX boundary** — `.vscodeignore` keeps the shipped package to the runtime
  surface (adapter + runtime exe + `node-pty`). See [INSTALL_AND_BUILD.md](INSTALL_AND_BUILD.md).
- **Repo-only support** — docs, `.vscode/`, tests, tools: tracked, never shipped.

---

## 2. File entry points — what starts where

| File | Role | Starts / owns |
|---|---|---|
| `package.json` | root manifest | `main` = `./extension-adapter/extension.js`; scripts `vscode:prepublish` (build runtime), `vsce:package` (build VSIX) |
| `extension-adapter/extension.js` | **JS adapter (Layer 1)** | `activate()`/`deactivate()`; `startRuntime()` spawns the runtime; the internal bridge HTTP server + `/vscode/*` and `/vscode/term/*` handlers; the `codeapi.restartRuntime` / `codeapi.showRuntimeLog` commands |
| `extension-adapter/bridge_core.js` | vscode-free adapter logic | `runExecBody` (the 7 `/exec` globals), `apisFromCommands` (fixed `/apis` descriptors), `resolveTermDefaults` (terminal defaults), `makeCodeApiTerm` (registry facade) |
| `python/codeapi_runtime_main.py` | **runtime entry (Layer 2)** | absolute-import wrapper: `from codeapi_vscode.runtime import main; raise SystemExit(main())` — the PyInstaller entry point |
| `python/codeapi_vscode/runtime.py` | runtime bootstrap | `create_server()`, `PortAllocator` (binds the next free port at/after 47337, refuses 3637–3646), `main()` |
| `python/codeapi_vscode/server/api_registry.py` | route wiring | `register_all()` attaches session, dynamic, mesh, vscode, term route groups |
| `python/codeapi_vscode/server/session_routes.py` | pure routes | `GET /health`, `GET /session` |
| `python/codeapi_vscode/server/vscode_routes.py` | adapter-backed routes | `/exec`, `/command/:id`, `/workspace/folders`, `/editor/active`, `/window/showMessage`, `/apis` |
| `python/codeapi_vscode/server/term_routes.py` | terminal routes | `GET/POST/DELETE /term/*` |
| `python/codeapi_vscode/vscode/adapter.py` | **bridge client** | `VsCodeAdapter` — HTTP client to the JS bridge; sends the token on every call; `_parse_ok` maps `{ok,value?}`→value / `{error}`→`AdapterError`; unreachable→`AdapterUnavailable` |
| `python/codeapi_vscode/discovery.py` | **discovery/session (Layer 3)** | `discover_sessions`, `classify_runtime`, `compute_flags`, `select_by_identity`, `TerminalRouter` |
| `python/tools/build_runtime.py` | build tool | PyInstaller onedir build → `bin/codeapi-runtime/` (invoked by `vscode:prepublish`) |
| `python/tools/discover_and_route.py` | ops CLI | standalone discovery table + terminal routing demo |
| `.vscodeignore` | **VSIX boundary** | decides what ships in the `.vsix` |
| `.vscode/tasks.json`, `.vscode/launch.json` | dev workflow | build/test tasks; Run-Extension + standalone-runtime debug configs |

### Wired modules

`server/api_registry.py` is the **single source of truth for what is served**. It
wires exactly five route modules — `session_routes`, `dynamic_endpoint_routes`,
`mesh_routes`, `vscode_routes`, `term_routes` — plus the `vscode/adapter.py` bridge
client. That is the entire served surface; there is no shadow scaffolding to
disambiguate.

> **History:** the Phase-9 skeleton stubs that once shadowed these route names —
> `server/{exec,command,editor,workspace,terminal}_routes.py`, the `terminal/`
> package, `vscode/{commands,editor,window,workspace}.py`, and
> `util/{async_tools,logging}.py` — raised `NotImplementedError`, were wired nowhere,
> and have been removed. `/exec`, `/command`, `/workspace`, `/editor`, and `/apis` all
> live in `vscode_routes.py`; `/term/*` in `term_routes.py`. Trust `api_registry.py`,
> never a filename, for what runs.

---

## 3. Full execution flow

### Adapter-backed request (e.g. `POST /exec`, `/command/:id`, `/apis`)

```
external caller
  → public Python HTTP route        server/vscode_routes.py  (handler + _guarded)
  → VsCodeAdapter.<method>          vscode/adapter.py  (POST /vscode/* + X-CODEAPI-BRIDGE-TOKEN)
  → internal JS bridge dispatch     extension-adapter/extension.js
  → vscode.* API  OR  node-pty      (Layer 0, the only place these run)
  → bridge response {ok,value?}/{error}
  → Python wire shaping             server/vscode_routes.py + http_server.py
  → external response               (unwrapped /exec, wrapped /command, 503/500/404 on error)
```

- **Pure routes** (`/health`, `/session`, dynamic endpoints, mesh) short-circuit at
  the Python layer — no bridge hop.
- If the adapter is unconfigured/unreachable, `_guarded` returns **503**
  `{"error":"vscode adapter not available"}` (never 404).

### Terminal request

```
external caller
  → POST /term/create               server/term_routes.py
  → VsCodeAdapter.term_create       vscode/adapter.py → POST /vscode/term/create
  → node-pty.spawn(...)             extension.js termCreate (defaults via bridge_core.resolveTermDefaults)
  → visible VS Code terminal        if show:true → a Pseudoterminal named `codeapi:<id>` appears
  → POST /term/:id/write            types bytes into the pty                → {id,wrote}
  → GET  /term/:id/read             returns the raw screen buffer          → {id,bytes,buffer}
  → GET  /term/list                 liveness: alive:false + integer exitCode (node-pty onExit)
  → DELETE /term/:id                kill + dispose + remove from registry  → {id,killed}
```

**Liveness rule:** exit is observed from `/term/list` (`alive:false` **and** an integer
`exitCode`), never inferred from `DELETE` (which returns `killed:false` if the process
already exited).

---

## 4. Endpoint → logic map

| Public route | Python handler | `VsCodeAdapter` method | JS bridge route | Final owner | Response shape |
|---|---|---|---|---|---|
| `GET /health` | `server/session_routes.py` | — (pure) | — | Python | `{"status":"ok",…}` |
| `GET /session` | `server/session_routes.py` | — (pure) | — | Python | `{serverPort,pid,workspaceUri,windowId:0,…,vscodeSessionId,extensionHostPid,codeapiWindowId}` |
| `POST /exec` | `server/vscode_routes.py` | `exec_js` | `POST /vscode/exec` | JS eval (`bridge_core.runExecBody`) | **unwrapped** value; `undefined`→empty body; `null`→`null` |
| `POST /command/:id` | `server/vscode_routes.py` | `command` | `POST /vscode/command` | `vscode.commands.executeCommand` | **wrapped** `{"success":true,"result":<v>}` (`result` omitted when `undefined`) |
| `GET /workspace/folders` | `server/vscode_routes.py` | `workspace_folders` | `GET /vscode/workspace/folders` | `vscode.workspace` | array of folders |
| `GET /editor/active` | `server/vscode_routes.py` | `editor_active` | `GET /vscode/editor/active` | `vscode.window.activeTextEditor` | object, or `null` when no editor |
| `POST /window/showMessage` | `server/vscode_routes.py` | `show_message` | `POST /vscode/window/message` | `vscode.window.show*Message` (**blocking**) | `{"success":true}` |
| `GET /apis` | `server/vscode_routes.py` | `apis` | `GET /vscode/apis` | command registry + 13 fixed descriptors (`bridge_core.apisFromCommands`) | array of descriptors |
| `GET /term/list` | `server/term_routes.py` | `term_list` | `GET /vscode/term/list` | node-pty registry | `[{id,pid,bytes,alive,exitCode}]` |
| `POST /term/create` | `server/term_routes.py` | `term_create` | `POST /vscode/term/create` | `node-pty.spawn` (+ visible pty if `show:true`) | `{id,shell,cwd,pid}` |
| `POST /term/:id/write` | `server/term_routes.py` | `term_write` | `POST /vscode/term/:id/write` | `pty.write` | `{id,wrote}` (bytes accepted, **not** success) |
| `GET /term/:id/read` | `server/term_routes.py` | `term_read` | `GET /vscode/term/:id/read` | buffer ring | `{id,bytes,buffer}` (raw ANSI) |
| `POST /term/:id/resize` | `server/term_routes.py` | `term_resize` | `POST /vscode/term/:id/resize` | `pty.resize` | `{id,cols,rows}` |
| `DELETE /term/:id` | `server/term_routes.py` | `term_delete` | `DELETE /vscode/term/:id` | `pty.kill` + dispose | `{id,killed}` |

Exact byte shapes for each route live in [WIRE_CONTRACT.md](WIRE_CONTRACT.md); the
terminal contract (defaults, liveness) lives in [TERM_CONTRACT.md](TERM_CONTRACT.md);
the bridge envelope in [ADAPTER_BRIDGE_CONTRACT.md](ADAPTER_BRIDGE_CONTRACT.md).
