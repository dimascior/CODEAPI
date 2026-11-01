# CODEAPI Adapter Bridge Contract

The **Python-runtime ↔ JS-adapter protocol** for the adapter-backed (non-terminal)
public routes. This is the **running contract**: `package.json.main` is the adapter
(`./extension-adapter/extension.js`), which serves the token-guarded internal
`/vscode/*` bridge; the Python runtime routes the adapter-backed public routes to it.

Wire shapes: [WIRE_CONTRACT.md](WIRE_CONTRACT.md). Terminal routes: [TERM_CONTRACT.md](TERM_CONTRACT.md).
Running system + layers: [FILE_FLOW.md](FILE_FLOW.md),
[CODEAPI_RUNTIME_ARCHITECTURE.md](CODEAPI_RUNTIME_ARCHITECTURE.md).

The shapes this contract enforces:

- `POST /exec` returns the operation value **unwrapped**.
- `POST /command/:id` returns **wrapped** `{"success":true,"result":<value>}`, and
  **omits `result`** when the command result is `undefined`.
- `POST /window/showMessage` is **blocking** — the HTTP response does not return until
  the VS Code toast is dismissed — and then returns `{"success":true}` (the selected
  item is not surfaced).

---

## 1. Two HTTP surfaces

| # | Surface | Bind | Who calls it | Purpose |
|---|---|---|---|---|
| 1 | **Public CODEAPI service** | `127.0.0.1:CODEAPI_PY_PORT` — **47337+** (reserved band, outside the legacy window band 3637–3646) | external clients (curl, `python python/tools/discover_and_route.py`, `codeapi_vscode.discovery`) | the stable public wire contract |
| 2 | **Internal JS adapter bridge** | `127.0.0.1:CODEAPI_EXTENSION_PORT` — **ephemeral** (`listen(0)`), loopback | **only** the Python runtime (`vscode/adapter.py`) | execute the irreducible `vscode.*` / `node-pty` leaf calls |

```
external client
      │  GET /workspace/folders          (public route, 47337+)
      ▼
┌─────────────────────────┐
│ Python CODEAPI service  │  pure routes answered here (health/session/dynamic/mesh)
│  (47337+)               │
│                         │  adapter-backed routes ↓
│  vscode/adapter.py ─────┼── POST /vscode/... ──▶ ┌────────────────────┐
└─────────────────────────┘   (ephemeral loopback) │ JS adapter bridge  │
      ▲                                             │ (extension host)   │
      │  shaped public response                     │  runs vscode.* /   │
      └─────────────────────────────────────────────┤  node-pty          │
                                                     └────────────────────┘
```

- **Pure route:** `client → Python service → Python → client`. Never touches the bridge.
- **Adapter-backed route:** `client → Python service → vscode/adapter.py → HTTP →
  JS bridge → vscode.* in the host → {ok,value}/{error} → Python shapes it → client`.

---

## 2. Security boundary (normative)

1. **Python never imports or calls `vscode.*`.** The `codeapi_vscode` package has no
   `vscode` dependency. It may only *ask the adapter to* run a `vscode.*` / `node-pty`
   operation over the bridge.
2. **The JS adapter is the only component that executes `vscode.*` and `node-pty`.** It
   is the single trust root for host access.
3. **All adapter calls are loopback-only.** Both surfaces bind `127.0.0.1`. The bridge
   port is **ephemeral** and handed to the Python child solely via
   `CODEAPI_EXTENSION_PORT` — an unguessable, per-launch capability.
4. **Per-launch bridge token — REQUIRED.** Loopback is necessary but **not sufficient**
   (another loopback process could race/guess the port), so every bridge request MUST
   carry a per-launch token:

   > Every adapter bridge request MUST include the per-launch `CODEAPI_BRIDGE_TOKEN`.
   >
   > **Header:** `X-CODEAPI-BRIDGE-TOKEN: <token>`
   >
   > **Token source:**
   > - the JS adapter **generates** the token on activation (`crypto.randomBytes`);
   > - the JS adapter **passes** it to Python as `CODEAPI_BRIDGE_TOKEN` (spawn env);
   > - the Python adapter client **sends** it on **every** bridge request.
   >
   > **Failure:** missing or wrong token → `403 {"error":"forbidden"}`.

   Env passed adapter → Python: `CODEAPI_BRIDGE_TOKEN`, alongside `CODEAPI_PY_PORT`,
   `CODEAPI_EXTENSION_PORT`, `CODEAPI_WORKSPACE_URI`, `CODEAPI_EXTENSION_PATH`, and the
   identity vars `CODEAPI_VSCODE_SESSION_ID` / `CODEAPI_EXTENSION_HOST_PID` /
   `CODEAPI_WINDOW_ID`.

---

## 3. Route map — public service ↔ internal bridge

| Public service route (47337+) | Internal bridge route (ephemeral) | Public shape rule |
|---|---|---|
| `POST /exec` (`text/plain` JS body) | `POST /vscode/exec` `{code}` | **unwrapped** value |
| `POST /command/:commandId` `{args}` | `POST /vscode/command` `{id,args}` | **wrapped** `{success:true,result?}` |
| `GET /workspace/folders` | `GET /vscode/workspace/folders` | array, per WIRE_CONTRACT |
| `GET /editor/active` | `GET /vscode/editor/active` | object / `null`, per WIRE_CONTRACT |
| `POST /window/showMessage` `{message,type}` | `POST /vscode/window/message` `{message,type}` | **blocking**, then `{success:true}` |
| `GET /apis` | `GET /vscode/apis` | array of descriptors, per WIRE_CONTRACT |

Pure routes (`/health`, `/session`, `/add-endpoint`, `/remove-endpoint`, dynamic GET,
`/mesh/*`) never reach the bridge. The **terminal** routes `/term/*` are also
adapter-backed and live — their bridge is `/vscode/term/*`; their contract is
[TERM_CONTRACT.md](TERM_CONTRACT.md).

---

## 4. The bridge envelope (uniform) — and where wrap/unwrap lives

The bridge speaks **one** envelope for every route. The public wrap/unwrap difference
is **entirely a Python-side shaping decision** — the bridge is uniform. That is why
`/exec` (unwrapped) and `/command` (wrapped) share one mechanism.

Every `/vscode/*` request carries `X-CODEAPI-BRIDGE-TOKEN` (§2.4). A request without a
valid token is rejected with `403 {"error":"forbidden"}` **before** any `vscode.*` runs.

**Bridge success (HTTP 200):**
```json
{ "ok": true, "value": <any JSON> }
```
`value` is **omitted** when the underlying result is `undefined` (the adapter builds
`{ok:true,value:result}` and `JSON.stringify` drops the `undefined`-valued key). A
`null` result is transmitted **present** as `"value": null`. This present-vs-absent
distinction is load-bearing for `/command` and `/editor/active`.

**Bridge error (HTTP 4xx/5xx):** `{ "error": "<message>" }`
**Bridge unknown route (HTTP 404):** `{ "error": "Cannot <METHOD> <path>" }`

### Python shaping per route (from the same envelope)

| Route | Bridge → Python | Python → public client |
|---|---|---|
| `/exec` | `{ok:true, value}` (absent if `undefined`) | return `value` **unwrapped**, verbatim JSON; **empty body** when the value was absent (JS `undefined`); `null` stays `null`. |
| `/command/:id` | `{ok:true, value}` (absent if `undefined`) | build `{"success":true}`; add `"result": value` only if `value` was present. |
| `/workspace/folders` | `{ok:true, value:[{uri,name,index}]}` | return `value` unwrapped (the array). |
| `/editor/active` | `{ok:true, value:<obj\|null>}` | return `value` unwrapped. No active editor is `null` (present), not `undefined`. |
| `/window/showMessage` | `{ok:true, value:<selected\|undefined>}` after dismissal | return `{"success":true}` always; `value` intentionally discarded. |
| `/apis` | `{ok:true, value:[…descriptors…]}` | return `value` unwrapped (the array). |

> **This is the whole trick.** `/exec` unwrapped and `/command` wrapped are the *same*
> bridge call shaped two different ways in Python. Keeping the bridge envelope uniform
> means the public wrap/unwrap rules live in exactly one place
> (`server/vscode_routes.py`), where the [WIRE_CONTRACT.md](WIRE_CONTRACT.md) shapes are
> enforced — not smeared across JS and Python.

---

## 5. Blocking behavior for `showMessage`

- The bridge handler `await`s `vscode.window.show{Information,Warning,Error}Message(...)`
  and does **not** respond until that promise resolves (the user dismisses/acts on the
  toast). The Python→bridge request therefore stays open the whole time.
- `vscode/adapter.py` uses a long/disabled read timeout for this one route; a short
  timeout would raise client-side even though the server is healthy and still awaiting
  the user. The public `POST /window/showMessage` inherits the block — the Python route
  must not impose its own short timeout.

---

## 6. `/apis` behavior

- The bridge handler for `GET /vscode/apis` builds, in the extension host, the **13
  fixed API descriptors** (`{category, method, parameters}`, plus `description` for
  `command` entries) **prepended** to the live command-registry rows
  (`bridge_core.apisFromCommands`).
- **The response is window-dependent.** The command-registry count differs per window
  (thousands of rows). Callers and tests **must not assume a fixed count** — assert
  structure (the 13 fixed descriptors + command rows in the shape
  `{category:"command",method:id,parameters:["...args"],description:"Execute VSCode command: "+id}`),
  not length.
- Python passes the array through unwrapped.

---

## 7. Error model

| Situation | Bridge response | Public service response |
|---|---|---|
| Missing / invalid bridge token | `403 {"error":"forbidden"}` | n/a (internal; public requests never carry the token) |
| `vscode.*` leaf throws | `500 {"error":"<message>"}` | `500 {"error":"<message>"}` (JSON) |
| Unknown `/vscode/*` route | `404 {"error":"Cannot <METHOD> <path>"}` | n/a (internal) |
| Unknown **public** route | n/a | `404 {"error":"..."}` |
| Public handler error | n/a | `500 {"error":"<message>"}` (JSON) — never leak a stack |
| Adapter unconfigured/unreachable | — | `503 {"error":"vscode adapter not available"}` (adapter-backed + `/term/*` only) |

---

## 8. Standalone mode (no adapter)

The runtime can run with **no adapter** (no `--adapter-port`, no reachable bridge), e.g.
`python -m codeapi_vscode.runtime` or the isolated packaged-exe validation:

- **Pure routes are unaffected** — they never needed the bridge.
- **Adapter-backed routes and `/term/*` return `503 {"error":"vscode adapter not available"}`**
  — a clean, documented failure, not a crash and not a hang.

This is what lets `python/tests/test_runtime_pure.py` / `test_runtime_cli.py` run without
VS Code.

---

## 9. Where each piece lives

| Artifact | Path | Role |
|---|---|---|
| Python adapter client | `python/codeapi_vscode/vscode/adapter.py` | `VsCodeAdapter` — HTTP client to the bridge; sends `X-CODEAPI-BRIDGE-TOKEN` on every call; long timeout for `showMessage`; `AdapterUnavailable → 503`, `AdapterError → 500` |
| Python public routes | `python/codeapi_vscode/server/vscode_routes.py` | the 6 adapter-backed public routes + the shaping in §4 |
| Route registration | `python/codeapi_vscode/server/api_registry.py` | registers `vscode_routes` (and `term_routes`) with the adapter |
| JS bridge handlers | `extension-adapter/extension.js` | the `/vscode/*` and `/vscode/term/*` handlers; token gate; `bridge_core.js` holds the vscode-free logic (`runExecBody`, `apisFromCommands`, `resolveTermDefaults`, `makeCodeApiTerm`) |

## 10. Tests

- **Bridge-unit (no VS Code):** a fake adapter bridge asserts `vscode/adapter.py` +
  `server/vscode_routes.py` produce byte-exact public shapes
  (`python/tests/test_adapter_client.py`, `test_vscode_routes.py`).
- **Standalone:** `test_runtime_pure.py` / `test_runtime_cli.py` — pure routes answer,
  adapter-backed + `/term/*` → 503.
- **JS:** `extension-adapter/tests/test_bridge_core.js` proves the exec globals, the 13
  `/apis` descriptors, terminal defaults, and the `codeApiTerm` facade.
