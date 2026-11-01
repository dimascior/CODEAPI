# CODEAPI Wire Contract

**The byte-level request/response shapes the Python runtime serves** on every public
HTTP route. This is the wire the runtime speaks; clients depend on these exact shapes.

> **Binding rule:** the runtime binds **outside the legacy window band 3637–3646**, in
> the reserved band starting at **47337** (discovery scan window `47337–47400`). This
> contract defines the byte shapes the runtime serves there.

Companions: [ADAPTER_BRIDGE_CONTRACT.md](ADAPTER_BRIDGE_CONTRACT.md) (the Python↔JS
bridge envelope), [TERM_CONTRACT.md](TERM_CONTRACT.md) (`/term/*`),
[FILE_FLOW.md](FILE_FLOW.md) (endpoint→logic map).

## Conventions

- **Transport:** loopback HTTP, compact JSON responses (`separators=(",",":")`, insertion
  order preserved) unless noted.
- **`/exec` request body is `text/plain`** (raw JavaScript); everything else that takes a
  body uses `application/json`.
- **`/exec` is unwrapped:** it returns the operation's value **directly** (e.g. `"CODEAPI"`,
  `2`, `[…]`, `{…}`, `null`) — not in an envelope. An `undefined` result is an **empty
  `200` body**; `null` is the literal `null`. **`/command` does NOT share this rule** — it
  returns a wrapped `{ "success": true, "result": <value> }` envelope.
- **Error envelopes:** handler-level errors return JSON `{ "error": "<message>" }` with
  4xx/5xx; unknown routes return `404`. Adapter-backed routes return
  `503 {"error":"vscode adapter not available"}` when no adapter is configured/reachable.

---

## Pure routes (no `vscode` dependency — answered entirely in Python)

### `GET /health`
- Request: none.
- Response `200`: `{ "status": "ok", "timestamp": "<ISO-8601 UTC>" }` (`timestamp` per call).

### `GET /session`
- Request: none.
- Response `200` — example (adapter-launched runtime on 47337):
  ```json
  {
    "id": "…",
    "pid": 33468,
    "workspaceUri": "file:///path/to/your/workspace",
    "windowId": 0,
    "capabilities": [],
    "lastSeen": "<ISO-8601 UTC>",
    "serverPort": 47337,
    "meshPeers": [],
    "vscodeSessionId": "…",
    "extensionHostPid": 28620,
    "codeapiWindowId": "3194881e-…",
    "codeapiWorkspaceId": "3194881e-…",
    "codeapiRuntimeId": "b41d2c7f-…"
  }
  ```
  - `id` **rotates** per launch under a stable `pid`; `capabilities` is **not** terminal
    state (always `[]`); `serverPort` is the stable handle; `workspaceUri` is a
    percent-encoded `file://` URI; `lastSeen` is regenerated per call.
  - **Adapter-pinned identity — additive fields** (appended after `meshPeers`;
    present-as-`null` in the standalone/no-adapter runtime):
    - **`codeapiWorkspaceId`** — UUID persisted in `context.workspaceState` under
      `codeapi.windowId`; **durable across reloads but per-WORKSPACE** (windows on one
      workspace share it — a grouping key, not a unique window target).
    - **`codeapiWindowId`** — LEGACY NAME for the same value, kept for compatibility;
      semantically a workspace identity.
    - **`codeapiRuntimeId`** — fresh UUID per adapter activation, never persisted;
      **the unique live-window target** (rotates on reload — rediscover after).
    - **`vscodeSessionId`** (`vscode.env.sessionId`) — a VS Code-provided session
      correlation signal; secondary target key only (VS Code guarantees per-session
      uniqueness, not a CODEAPI-owned guarantee between simultaneous windows) — and
      **`extensionHostPid`** (the JS adapter's `process.pid` — **not** the Python
      `pid`). `windowId` stays `0` and is **not** repurposed.
  - **Discovery selection precedence:** `codeapiRuntimeId` > `vscodeSessionId` >
    `codeapiWindowId`/`codeapiWorkspaceId` > `extensionHostPid` > `workspaceUri` >
    `serverPort` — every tier is strict (multiple matches raise; no silent first-match).

### `POST /add-endpoint`
- Request: `{ "path": "/x", "response": <any JSON> }`
- Response `200`: `{ "success": true, "message": "Endpoint /x registered." }`

### `GET <registered dynamic path>`
- Response `200`: `{ "success": true, "dynamic": true, "path": "/x", "response": <value> }`

### `POST /remove-endpoint`
- Request: `{ "path": "/x" }`
- Response `200`: `{ "success": true, "message": "Endpoint /x removed." }`; `404` `Endpoint not found.` if unregistered.

### `GET /mesh/peers`
- Response `200`: `[]` (mesh discovery is inert — always empty).

### `POST /mesh/broadcast/:endpoint`
- Request: `{ … }`; Response `200`: `{ "success": true, "results": [] }` (empty with no peers).

---

## Adapter-backed routes (routed to the JS bridge; see ADAPTER_BRIDGE_CONTRACT)

### `POST /exec`
- Request: **`text/plain`** body = JavaScript function body (e.g. `return vscode.workspace.name`).
  Globals in scope: `vscode`, `Buffer`, `os`, `process`, `cp` (child_process), `nodePty`,
  `codeApiTerm`. The body may `return` a value or a Promise (it is awaited).
- Response `200`: the **unwrapped** return value. `return 1+1` → `2`; `return null` →
  `null`; **`return undefined` → empty `200` body** (`Content-Length: 0`).
- Errors: `500 { "error": "<message>" }` on a thrown exception; `503` if no adapter.

### `POST /command/:commandId`
- Request: `{ "args": [ … ] }`
- Response `200` — **wrapped** (not unwrapped like `/exec`):
  - value returned → `{ "success": true, "result": <value> }`
  - `undefined`/void → `{ "success": true }` (the `result` key is omitted).

### `GET /workspace/folders`
- Response `200`: `[ { "uri": "file:///…", "name": "…", "index": 0 } ]`

### `GET /editor/active`
- Response `200`: `null` if no active editor, else
  `{ "document": { "uri","fileName","languageId","lineCount" }, "selection": { "start":{line,character}, "end":{…} } }`.

### `POST /window/showMessage`
- Request: `{ "message": "…", "type": "info|warning|error" }`
- **Blocking route:** the handler awaits the VS Code toast, so the HTTP response does not
  return until the user dismisses/acts on it. Clients must treat it as a long-poll.
- Response `200`: `{ "success": true }` (the selected item is not surfaced).

### `GET /apis`
- Response `200`: an array of `{ category, method, parameters }` (plus `description` for
  `command` entries). It is the **13 fixed descriptors** (categories `workspace, window,
  editor, commands, languages, extensions`) **prepended** to the command-registry rows
  (`{category:"command",method:id,parameters:["...args"],description:"Execute VSCode command: "+id}`).
- **Window-dependent count** — assert structure, not length.

### `POST /exec-with-action` — **not served**
- The runtime returns `404`. It was a standalone-only helper (the `standalone-exec-with-action.js`
  test server), retired in Phase 15. It is not part of the runtime.

---

## Terminal routes `/term/*`

Live and adapter-backed; full contract (routes, `create` defaults, liveness rule) in
[TERM_CONTRACT.md](TERM_CONTRACT.md). Summary of `200` shapes:

| Route | Request | Response `200` |
|---|---|---|
| `GET /term/list` | — | `[ { "id","pid","bytes","alive","exitCode","surface" } ]` |
| `POST /term/create` | `{ id?, cwd?, shell?, args?, cols?, rows?, maxBytes?, show? }` | `{ "id","shell","cwd","pid","surface" }` |
| `POST /term/:id/write` | `{ data, enter? }` | `{ "id","wrote" }` (bytes accepted, **not** success) |
| `GET /term/:id/read` | `?tail=N&clear=bool` | `{ "id","bytes","buffer" }` (raw ANSI) |
| `POST /term/:id/resize` | `{ cols, rows }` | `{ "id","cols","rows" }` |
| `DELETE /term/:id` | — | `{ "id","killed" }` |

`alive`/`exitCode` come from the adapter's node-pty `onExit` (see TERM_CONTRACT §liveness);
unknown `:id` → `404 {"error":"Terminal <id> not found"}`. `surface` is the hard
process-vs-terminal discriminator (`"vscode-terminal"` = surfaced as the visible
integrated terminal `codeapi:<id>`; `"process"` = headless PTY, no terminal UI presence)
— see TERM_CONTRACT §4; only `surface:"vscode-terminal"` can support a "terminal open in
the window" claim.
